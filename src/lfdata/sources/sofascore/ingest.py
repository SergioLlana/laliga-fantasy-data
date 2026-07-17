"""Ingesta bajo demanda de SofaScore: historial completo de un jugador.

``ingest_player`` resuelve un jugador (por canonical_id, id de SofaScore o
nombre), recorre **todas** sus temporadas en cualquier liga y publica dos tablas
curadas:

- ``player_season_stats`` — grano jugador-temporada: el agregado de 115 campos.
- ``player_match_stats`` — grano jugador-partido: nota y métricas de evento
  (minutos, pases, remates, goles, asistencias, xG...), una petición por partido.

Cada fila lleva ``canonical_id`` si el id de SofaScore ya está mapeado (ADR 0001);
si no, la fila conserva solo el ``sofascore_player_id`` y se resuelve en una ronda
posterior de ``lfdata map`` (que propone el canónico desde el catálogo
``sofascore_players``) y un re-estampado con ``lfdata curate sofascore-canonical``.

Es el modo más simple del paso 3 (docs/implementation/03) y desbloquea el
baseline de fichajes (paso 5): dado un fichaje de otra liga, un comando trae su
historial completo.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from lfdata.mappings import MappingStore
from lfdata.mappings.matcher import birthdate_matches
from lfdata.mappings.normalize import name_compatible
from lfdata.sources.http import HttpTransport, SourceHTTPError, scrapeops_proxy_from_env
from lfdata.sources.ingestion import IngestResult, PlayerFailure
from lfdata.sources.sofascore.client import PROXY_OVERFLOW, WAIT_SECONDS, SofaScoreClient
from lfdata.sources.sofascore.models import (
    CalendarEvent,
    EventsResponse,
    LineupsResponse,
    SeasonRating,
)
from lfdata.storage import Storage

logger = logging.getLogger(__name__)

SOURCE = "sofascore"
_CANONICAL_RE = re.compile(r"^p\d+$")

# ``search/all`` mezcla deportes; solo el fútbol es candidato a un jugador de La
# Liga (un homónimo de baloncesto se descarta por aquí, ver ADR 0001).
FOOTBALL = "football"

# Métricas de conteo por partido: SofaScore omite el campo cuando vale cero, así
# que una ausencia (con el evento sí descargado) significa cero.
_MATCH_COUNT_FIELDS = {
    "goals": "goals",
    "assists": "goalAssist",
    "shots": "totalShots",
    "shots_on_target": "onTargetScoringAttempt",
    "passes": "totalPass",
    "accurate_passes": "accuratePass",
    "key_passes": "keyPass",
    "crosses": "totalCross",
    "touches": "touches",
    "ball_recovery": "ballRecovery",
}
# Métricas continuas: la ausencia es dato faltante (nulo), no cero —p. ej. LaLiga2
# no publica ``expectedGoals``, y los minutos solo faltan si el evento no se pudo
# descargar—.
_MATCH_FLOAT_FIELDS = {
    "minutes": "minutesPlayed",
    "expected_goals": "expectedGoals",
    "expected_assists": "expectedAssists",
}

# Campos del agregado por temporada que no son métricas (metadatos internos de
# SofaScore); se excluyen de ``player_season_stats``.
_SEASON_SKIP = {"id", "type", "statisticsType"}

_MATCH_KEY = "player_event"
_SEASON_KEY = "sofascore_player_id"


def ingest_player(
    storage: Storage,
    query: str,
    *,
    mappings_dir: str = "mappings",
    transport: HttpTransport | None = None,
) -> IngestResult:
    """Descarga el historial completo de un jugador y lo cura.

    ``query`` puede ser un ``canonical_id`` (``p00001``), un id de SofaScore
    (numérico) o un nombre a buscar. Devuelve las filas escritas por tabla y los
    partidos que fallaron (un evento que la fuente no sirve se salta sin abortar).
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )
    client = SofaScoreClient(transport, storage.raw)
    store = MappingStore(Path(mappings_dir))
    store.load()
    canonical_by_sofascore = MappingStore.canonical_by_source(store.players, SOURCE)

    player_id, name = _resolve_player(client, query, store)
    canonical_id = canonical_by_sofascore.get(str(player_id), "")

    seasons = client.fetch_seasons(player_id).unique_tournament_seasons
    result = IngestResult(
        rows={"player_season_stats": 0, "player_match_stats": 0},
        stats={"temporadas": 0, "sin_mapping": 0 if canonical_id else 1},
    )

    for tournament in seasons:
        ut_id = tournament.unique_tournament.id
        for season in tournament.seasons:
            partition = {"competition": str(ut_id), "season": str(season.id)}
            common = {
                "canonical_id": canonical_id,
                "sofascore_player_id": player_id,
                "season_year": season.year,
                "source": SOURCE,
            }
            result.stats["temporadas"] += 1

            overall = client.fetch_overall(player_id, ut_id, season.id)
            season_row = {**common, **_season_metrics(overall.statistics)}
            storage.curated.upsert_table(
                "player_season_stats",
                pd.DataFrame([season_row]),
                key=_SEASON_KEY,
                partition=partition,
            )
            result.rows["player_season_stats"] += 1

            ratings = client.fetch_ratings(player_id, ut_id, season.id).season_ratings
            match_rows = []
            for rating in ratings:
                stats, failure = _event_stats(client, rating.event_id, player_id)
                if failure is not None:
                    result.failures.append(failure)
                match_rows.append(_match_row(rating, stats, player_id, common))
            if match_rows:
                storage.curated.upsert_table(
                    "player_match_stats",
                    pd.DataFrame(match_rows),
                    key=_MATCH_KEY,
                    partition=partition,
                )
                result.rows["player_match_stats"] += len(match_rows)

            logger.info(
                "sofascore %s ut=%d season=%s (%s): %d partidos",
                name,
                ut_id,
                season.id,
                season.year,
                len(match_rows),
            )

    return result


def backfill_league_season(
    storage: Storage,
    tournament_id: int,
    season_id: int,
    *,
    season_year: str | None = None,
    max_matches: int | None = None,
    max_pages: int | None = None,
    mappings_dir: str = "mappings",
    transport: HttpTransport | None = None,
) -> IngestResult:
    """Backfill de una liga-temporada a ``player_match_stats`` (todos los jugadores).

    Recorre el calendario del torneo y, por cada partido terminado, pide sus
    alineaciones (una petición: trae la estadística de evento de los 22+
    jugadores) y escribe una fila por jugador que jugó. Comparte tabla, partición
    y clave con el modo bajo demanda.

    Reanudable: un partido cuya alineación ya está en ``raw/`` no se re-descarga
    (docs/implementation/03). ``max_matches``/``max_pages`` acotan una prueba.
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )
    client = SofaScoreClient(transport, storage.raw)
    store = MappingStore(Path(mappings_dir))
    store.load()
    canonical_by_sofascore = MappingStore.canonical_by_source(store.players, SOURCE)
    partition = {"competition": str(tournament_id), "season": str(season_id)}

    events = _finished_events(client, tournament_id, season_id, max_pages)
    result = IngestResult(
        rows={"player_match_stats": 0},
        stats={"partidos": 0, "partidos_saltados": 0, "jugadores_sin_mapping": 0},
    )
    unmapped: set[int] = set()
    processed = 0
    for event in events:
        if max_matches is not None and processed >= max_matches:
            break
        already_raw = storage.raw.last_download_date("sofascore", "event-lineups", str(event.id))
        if already_raw is not None:
            result.stats["partidos_saltados"] += 1
            continue
        processed += 1
        try:
            lineups = client.fetch_lineups(event.id)
        except SourceHTTPError as error:
            result.failures.append(PlayerFailure(f"event {event.id}", error.url, error.status))
            continue
        rows = _lineup_rows(event, lineups, canonical_by_sofascore, season_year, unmapped)
        if rows:
            storage.curated.upsert_table(
                "player_match_stats",
                pd.DataFrame(rows),
                key=_MATCH_KEY,
                partition=partition,
            )
            result.rows["player_match_stats"] += len(rows)
        result.stats["partidos"] += 1
        logger.info(
            "sofascore backfill ut=%d season=%s partido %d: %d jugadores",
            tournament_id,
            season_id,
            event.id,
            len(rows),
        )

    result.stats["jugadores_sin_mapping"] = len(unmapped)
    return result


def rebuild_matches(storage: Storage, mappings_dir: str = "mappings") -> IngestResult:
    """Re-cura ``player_match_stats`` desde ``raw/`` sin ninguna petición (ADR 0003, #80).

    El backfill (:func:`backfill_league_season`) salta el partido cuyo lineup ya
    está en ``raw/``: evita la descarga, pero **también** el curado. Esto reconstruye
    la tabla releyendo ``event-lineups`` (las alineaciones que el backfill ya bajó) y
    ``tournament-events`` (de donde salen competición, temporada y rival de cada
    partido), aplicando la lógica y los mappings vigentes.

    A diferencia de :func:`restamp_canonical` —que solo rellena el ``canonical_id``
    cruzando la tabla ya curada con los mappings— aquí se rehace la fila entera, así
    que recoge cualquier cambio en la lógica de curado, no solo la columna de join.

    Reescribe cada partición ``(competición, temporada)`` completa desde raw/ (refresh
    total, como :func:`build_catalog`): un partido cuyo lineup ya no esté en raw/
    desaparece de la tabla. No pide nada a la fuente, así que la cuota y ScrapeOps no
    se tocan.
    """
    store = MappingStore(Path(mappings_dir))
    store.load()
    canonical_by_sofascore = MappingStore.canonical_by_source(store.players, SOURCE)

    events, event_anomalies = _finished_events_from_raw(storage)
    rows_by_partition: dict[tuple[str, str], list[dict]] = defaultdict(list)
    unmapped: set[int] = set()
    lineup_anomalies = 0
    recured = 0
    for name, payload in storage.raw.iter_latest(SOURCE, "event-lineups"):
        try:
            event_id = int(name)
        except ValueError:
            continue
        meta = events.get(event_id)
        if meta is None:
            continue
        event, competition, season, season_year = meta
        try:
            lineups = LineupsResponse.model_validate_json(payload)
        except ValidationError:
            logger.warning("sofascore lineups ilegible: %s, se salta la re-cura", name)
            lineup_anomalies += 1
            continue
        rows = _lineup_rows(event, lineups, canonical_by_sofascore, season_year, unmapped)
        rows_by_partition[(competition, season)].extend(rows)
        recured += 1

    total = 0
    for (competition, season), rows in rows_by_partition.items():
        storage.curated.write_table(
            "player_match_stats",
            pd.DataFrame(rows),
            partition={"competition": competition, "season": season},
        )
        total += len(rows)

    anomalies: dict[str, int] = {}
    if event_anomalies:
        anomalies["tournament-events ilegible"] = event_anomalies
    if lineup_anomalies:
        anomalies["lineups ilegible"] = lineup_anomalies
    result = IngestResult(
        rows={"player_match_stats": total},
        stats={"partidos_recurados": recured, "jugadores_sin_mapping": len(unmapped)},
        anomalies=anomalies,
    )
    logger.info(
        "sofascore re-cura desde raw: %d partidos, %d filas, %d jugadores sin mapping",
        recured,
        total,
        len(unmapped),
    )
    return result


def _finished_events_from_raw(
    storage: Storage,
) -> tuple[dict[int, tuple[CalendarEvent, str, str, str | None]], int]:
    """De ``tournament-events`` en raw/: metadatos de cada partido terminado.

    Devuelve ``event_id -> (evento, competición, temporada, season_year)`` y el
    número de ficheros ilegibles. ``competición`` y ``temporada`` son las **mismas
    claves de partición** que escribió el backfill (id numérico del torneo e id
    opaco de temporada de SofaScore), leídas del propio evento; ``season_year`` es
    la etiqueta ``25/26`` que va como columna en la fila. Un evento sin torneo o sin
    temporada no se puede particionar y se salta.
    """
    events: dict[int, tuple[CalendarEvent, str, str, str | None]] = {}
    anomalies = 0
    for name, payload in storage.raw.iter_latest(SOURCE, "tournament-events"):
        try:
            response = EventsResponse.model_validate_json(payload)
        except ValidationError:
            logger.warning("sofascore tournament-events ilegible: %s, se salta", name)
            anomalies += 1
            continue
        for event in response.events:
            ut_id = event.unique_tournament_id
            if not event.finished or ut_id is None or event.season is None:
                continue
            events[event.id] = (event, str(ut_id), str(event.season.id), event.season.year)
    return events, anomalies


def backfill_league_season_for_year(
    storage: Storage,
    tournament_id: int,
    year: int,
    *,
    max_matches: int | None = None,
    max_pages: int | None = None,
    mappings_dir: str = "mappings",
    transport: HttpTransport | None = None,
) -> IngestResult:
    """Backfill de una liga-temporada indicando el **año de inicio** (2025 = 2025/26).

    Resuelve el año al id de temporada opaco de SofaScore y delega en
    :func:`backfill_league_season`. La convención (año de inicio) es la misma que
    usa Transfermarkt, para que ``--season 2025`` signifique 2025/26 en todas las
    fuentes.
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )
    client = SofaScoreClient(transport, storage.raw)
    season_id = resolve_season_id(client, tournament_id, year)
    return backfill_league_season(
        storage,
        tournament_id,
        season_id,
        season_year=season_year_label(year),
        max_matches=max_matches,
        max_pages=max_pages,
        mappings_dir=mappings_dir,
        transport=transport,
    )


def season_year_label(year: int) -> str:
    """Año de inicio → etiqueta estilo SofaScore: 2025 → ``25/26``."""
    return f"{year % 100:02d}/{(year + 1) % 100:02d}"


def season_start_year(year_label: str | None) -> int | None:
    """Inverso de :func:`season_year_label`: etiqueta de SofaScore → año de inicio.

    ``"25/26"`` → 2025, como ``--season`` en las demás fuentes. Una temporada de un
    solo año (``"2025"``) se toma tal cual; una etiqueta ilegible da ``None``.
    """
    if not year_label:
        return None
    head = year_label.split("/", 1)[0].strip()
    if not head.isdigit():
        return None
    value = int(head)
    if len(head) == 4:
        return value
    # Dos dígitos: 25 → 2025, 99 → 1999 (SofaScore no tiene ligas antes de los 90).
    return 2000 + value if value < 90 else 1900 + value


def resolve_season_id(client: SofaScoreClient, tournament_id: int, year: int) -> int:
    """Id de temporada de SofaScore para un año de inicio (2025 → temporada 25/26)."""
    target = season_year_label(year)
    seasons = client.fetch_tournament_seasons(tournament_id).seasons
    for season in seasons:
        if season.year == target:
            return season.id
    available = ", ".join(s.year for s in seasons if s.year) or "(ninguna)"
    raise ValueError(
        f"SofaScore no tiene la temporada {target} (año {year}) para el torneo "
        f"{tournament_id}. Disponibles: {available}."
    )


def _finished_events(
    client: SofaScoreClient,
    tournament_id: int,
    season_id: int,
    max_pages: int | None,
    *,
    dataset: str = "tournament-events",
) -> list[CalendarEvent]:
    """Todos los partidos terminados de la liga-temporada, paginando el calendario.

    ``dataset`` elige el prefijo raw del calendario (por defecto ``tournament-events``;
    el backfill de copas pasa ``cup-events``).
    """
    events: list[CalendarEvent] = []
    page = 0
    while True:
        response = client.fetch_events(tournament_id, season_id, page, dataset=dataset)
        events.extend(e for e in response.events if e.finished)
        page += 1
        if not response.has_next_page or (max_pages is not None and page >= max_pages):
            break
    return events


def _lineup_rows(
    event: CalendarEvent,
    lineups: LineupsResponse,
    canonical_by_sofascore: dict[str, str],
    season_year: str | None,
    unmapped: set[int],
) -> list[dict]:
    """Una fila de player_match_stats por jugador que jugó el partido."""
    date = (
        datetime.fromtimestamp(event.start_timestamp, tz=UTC).date().isoformat()
        if event.start_timestamp is not None
        else None
    )
    rows: list[dict] = []
    for side, is_home in ((lineups.home, True), (lineups.away, False)):
        opponent = event.away_team if is_home else event.home_team
        for entry in side.players:
            player_id = entry.player.id
            # Sin ``statistics`` (suplente que no jugó) o sin id: no genera fila.
            if player_id is None or not entry.statistics:
                continue
            canonical_id = canonical_by_sofascore.get(str(player_id), "")
            if not canonical_id:
                unmapped.add(player_id)
            rows.append(
                {
                    _MATCH_KEY: f"{player_id}_{event.id}",
                    "canonical_id": canonical_id,
                    "sofascore_player_id": player_id,
                    "season_year": season_year,
                    "source": SOURCE,
                    "event_id": event.id,
                    "date": date,
                    "opponent": opponent.name,
                    "opponent_id": opponent.id,
                    "is_home": is_home,
                    "rating": _as_number(entry.statistics.get("rating")),
                    **_event_metrics(entry.statistics),
                }
            )
    return rows


@dataclass(frozen=True)
class SearchIdentity:
    """Resultado de resolver la identidad de SofaScore por búsqueda + fecha.

    O ``verified_id`` (la fecha de nacimiento confirma a un candidato único de
    fútbol: listo para descargar por ID) o una lista de ``candidates`` para
    revisión con su ``motivo``. Nunca se descarga «al primero que salga».
    """

    verified_id: int | None
    candidates: list[dict] = field(default_factory=list)
    motivo: str = ""


def resolve_identity_by_search(
    client: SofaScoreClient, name: str, birth_date: str
) -> SearchIdentity:
    """Identidad de SofaScore de un fichaje fuera del catálogo, por ``search/all``.

    Filtra la búsqueda a fútbol y a nombre compatible (misma norma que el
    matcher). Con un único candidato, gasta **una** petición barata —la ficha
    ``player/{id}``, que trae la fecha de nacimiento— para contrastarla con la de
    Biwenger: solo si coincide se da por verificado. Con cero o varios candidatos
    no verifica a ciegas y devuelve los candidatos para encolarlos a revisión.
    """
    football = [
        player
        for player in client.search_players(name).players()
        if player.sport == FOOTBALL and name_compatible(name, player.name)
    ]
    candidates = [
        {
            "id": str(player.id),
            "name": player.name,
            "team": player.team.name if player.team else "",
            "birth_date": "",
        }
        for player in football
    ]
    if not football:
        return SearchIdentity(None, [], "sin-ficha")
    if len(football) > 1:
        return SearchIdentity(None, candidates, "varios-candidatos")

    only = football[0]
    profile_birth = _profile_birth_date(client, only.id)
    candidates[0]["birth_date"] = profile_birth
    if birthdate_matches(birth_date, profile_birth):
        return SearchIdentity(only.id, candidates, "")
    motivo = "fecha-discrepante" if birth_date and profile_birth else "sin-fecha-que-verificar"
    return SearchIdentity(None, candidates, motivo)


def _profile_birth_date(client: SofaScoreClient, player_id: int) -> str:
    """Fecha de nacimiento ISO de la ficha del jugador; vacío si no la publica."""
    timestamp = client.fetch_player(player_id).player.date_of_birth_timestamp
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, tz=UTC).date().isoformat()


def _resolve_player(
    client: SofaScoreClient, query: str, store: MappingStore
) -> tuple[int, str | None]:
    """Resuelve ``query`` a ``(id SofaScore, nombre)``.

    El club y la fecha de nacimiento de ``search/all`` no se usan (la fecha ni
    siquiera se publica, y el club puede estar desfasado): la identidad se resuelve
    después contra el catálogo con ``lfdata map``, no con la búsqueda.

    - ``canonical_id`` (``p\\d+``): busca su id de SofaScore en los mappings
      aprobados; si no lo tiene, es un error (no hay a quién descargar).
    - numérico: se toma como id de SofaScore directo.
    - texto: se busca por nombre y se toma el primer jugador del resultado.
    """
    if _CANONICAL_RE.match(query):
        rows = store.players[
            (store.players["fuente"] == SOURCE) & (store.players["canonical_id"] == query)
        ]
        if rows.empty:
            raise ValueError(
                f"{query} no tiene mapping a SofaScore todavía; "
                "ingiere por nombre o id de SofaScore."
            )
        return int(rows.iloc[0]["id_en_fuente"]), None

    if query.isdigit():
        return int(query), None

    players = client.search_players(query).players()
    if not players:
        raise ValueError(f"SofaScore no devolvió ningún jugador para {query!r}.")
    return players[0].id, players[0].name


def _event_stats(
    client: SofaScoreClient, event_id: int, player_id: int
) -> tuple[dict | None, PlayerFailure | None]:
    """Métricas del jugador en un evento; None + fallo si la fuente no lo sirve.

    Un evento que da 404 (o cae tras los reintentos) se registra como fallo y se
    salta: la fila del partido se escribe igual con la nota y métricas nulas, para
    no perder el partido por un hueco puntual de la fuente.
    """
    try:
        return client.fetch_event_player_stats(event_id, player_id).statistics, None
    except SourceHTTPError as error:
        logger.warning(
            "sofascore evento %d jugador %d: HTTP %d, se salta el detalle del partido",
            event_id,
            player_id,
            error.status,
        )
        return None, PlayerFailure(f"event {event_id}", error.url, error.status)


def _match_row(rating: SeasonRating, stats: dict | None, player_id: int, common: dict) -> dict:
    """Fila de player_match_stats: contexto del partido + métricas de evento."""
    opponent = rating.opponent
    date = (
        datetime.fromtimestamp(rating.start_timestamp, tz=UTC).date().isoformat()
        if rating.start_timestamp is not None
        else None
    )
    return {
        _MATCH_KEY: f"{player_id}_{rating.event_id}",
        **common,
        "event_id": rating.event_id,
        "date": date,
        "opponent": opponent.name if opponent else None,
        "opponent_id": opponent.id if opponent else None,
        "is_home": rating.is_home,
        "rating": rating.rating,
        **_event_metrics(stats),
    }


def _event_metrics(stats: dict | None) -> dict:
    """Métricas de evento neutras a partir del bloque ``statistics`` de SofaScore.

    Sin detalle (``stats`` None, p. ej. un evento que falló): todas nulas. Con
    detalle, la ausencia de un conteo es cero (SofaScore omite los ceros) y la de
    una métrica continua es dato faltante (nulo, p. ej. xG en LaLiga2).
    """
    metrics: dict = {}
    for neutral, key in _MATCH_COUNT_FIELDS.items():
        metrics[neutral] = None if stats is None else _as_number(stats.get(key, 0))
    for neutral, key in _MATCH_FLOAT_FIELDS.items():
        metrics[neutral] = None if stats is None else _as_number(stats.get(key))
    return metrics


def _season_metrics(statistics: dict) -> dict:
    """Campos numéricos del agregado por temporada, sin los metadatos internos."""
    metrics: dict = {}
    for key, value in statistics.items():
        if key in _SEASON_SKIP:
            continue
        number = _as_number(value)
        if number is not None:
            metrics[key] = number
    return metrics


def _as_number(value):
    """Deja pasar números; cualquier otra cosa (o ausencia) se vuelve None."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    return None
