"""Copa del Rey y competiciones UEFA: densidad de calendario de La Liga (issue #68).

De estas competiciones solo interesa, para el modelo de minutos, **cuándo** juega un
equipo de La Liga y **cuántos minutos** hacen sus jugadores entre semana (quien jugó
90' de Champions el miércoles llega distinto al domingo). No se ingiere su eventing
completo ni se mapea a los rivales extranjeros: "solo importa el partido y los minutos
de los nuestros".

Dos capas, como el resto (ADR 0003):

- **Descarga** (:func:`backfill_cups_for_year`): recorre el calendario de la copa y, de
  los partidos con al menos un equipo de La Liga, baja las alineaciones. Todo va a
  datasets raw propios (``cup-events``/``cup-lineups``) para no tocar ``tournament-events``
  /``event-lineups`` —que alimentan el catálogo de identidad y el eventing de La Liga—.
- **Curado** (:func:`rebuild_cups`), sin peticiones, desde raw/:
    - ``fixtures`` — un partido por fila (fecha + ambos equipos) de **todas** las
      competiciones de un equipo de La Liga (liga incluida, desde ``tournament-events``):
      así la densidad de calendario se calcula contra una sola tabla.
    - ``cup_minutes`` — minutos por jugador-partido **de los equipos de La Liga** en las
      copas, con ``canonical_id``. Los minutos de liga ya viven en ``player_match_stats``.

Quién es "de La Liga" se decide por presencia en el catálogo ``sofascore_teams`` de la
competición ``la-liga`` de esa temporada (lo publica ``build_catalog``), sin depender de
los mappings canónicos de equipo (que pueden no existir aún).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from lfdata.mappings import MappingStore
from lfdata.sources.http import HttpTransport, SourceHTTPError, scrapeops_proxy_from_env
from lfdata.sources.ingestion import IngestResult, PlayerFailure
from lfdata.sources.sofascore.client import (
    CALENDAR_TOURNAMENTS,
    COMPETITION_BY_ANY_TOURNAMENT,
    PROXY_OVERFLOW,
    WAIT_SECONDS,
    SofaScoreClient,
)
from lfdata.sources.sofascore.ingest import (
    _as_number,
    _finished_events,
    resolve_season_id,
    season_start_year,
)
from lfdata.sources.sofascore.models import CalendarEvent, EventsResponse, LineupsResponse
from lfdata.storage import Storage

logger = logging.getLogger(__name__)

SOURCE = "sofascore"
CUP_EVENTS_DATASET = "cup-events"
CUP_LINEUPS_DATASET = "cup-lineups"
FIXTURES_TABLE = "fixtures"
CUP_MINUTES_TABLE = "cup_minutes"

FIXTURES_COLUMNS = [
    "event_id",
    "date",
    "home_team_id",
    "home_team_name",
    "away_team_id",
    "away_team_name",
    "competition",
    "season",
]
CUP_MINUTES_COLUMNS = [
    "player_event",
    "canonical_id",
    "sofascore_player_id",
    "event_id",
    "date",
    "team_id",
    "opponent_id",
    "is_home",
    "is_starting",
    "minutes",
    "competition",
    "season",
]


def backfill_cups_for_year(
    storage: Storage,
    competition: str,
    year: int,
    *,
    max_matches: int | None = None,
    max_pages: int | None = None,
    transport: HttpTransport | None = None,
) -> IngestResult:
    """Baja a ``cup-events``/``cup-lineups`` los partidos de la copa con equipos de La Liga.

    Indica el **año de inicio** (2025 = 2025/26). Solo descarga (el curado es
    :func:`rebuild_cups`). Reanudable: un partido cuya alineación ya está en raw/ no se
    re-descarga. Un cruce sin ningún equipo de La Liga (Copa del Rey entre modestos, UEFA
    extranjero-vs-extranjero) ni se pide: ahorra cuota y no ensucia nada.
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )
    client = SofaScoreClient(transport, storage.raw)
    tournament_id = CALENDAR_TOURNAMENTS[competition]
    season_id = resolve_season_id(client, tournament_id, year)
    la_liga_teams = _la_liga_teams_by_season(storage).get(str(year), set())

    events = _finished_events(
        client, tournament_id, season_id, max_pages, dataset=CUP_EVENTS_DATASET
    )
    result = IngestResult(
        rows={},
        stats={"partidos": 0, "partidos_saltados": 0, "partidos_sin_la_liga": 0},
    )
    processed = 0
    for event in events:
        if not (event.home_team.id in la_liga_teams or event.away_team.id in la_liga_teams):
            result.stats["partidos_sin_la_liga"] += 1
            continue
        if max_matches is not None and processed >= max_matches:
            break
        if storage.raw.last_download_date(SOURCE, CUP_LINEUPS_DATASET, str(event.id)) is not None:
            result.stats["partidos_saltados"] += 1
            continue
        processed += 1
        try:
            client.fetch_lineups(event.id, dataset=CUP_LINEUPS_DATASET)
        except SourceHTTPError as error:
            result.failures.append(PlayerFailure(f"event {event.id}", error.url, error.status))
            continue
        result.stats["partidos"] += 1
        logger.info(
            "sofascore copas %s season=%s partido %d bajado",
            competition,
            season_id,
            event.id,
        )
    return result


def rebuild_cups(storage: Storage, mappings_dir: str = "mappings") -> IngestResult:
    """Publica ``fixtures`` y ``cup_minutes`` desde raw/, sin ninguna petición (ADR 0003).

    ``fixtures`` sale de ``tournament-events`` (liga) + ``cup-events`` (copas), filtrado a
    los partidos de equipos de La Liga. ``cup_minutes`` sale de ``cup-lineups``, solo de
    los jugadores de los equipos de La Liga, estampando el ``canonical_id`` con los
    mappings vigentes. Cada partición ``(competición, temporada)`` se reescribe entera.
    """
    store = MappingStore(Path(mappings_dir))
    store.load()
    canonical_by_sofascore = MappingStore.canonical_by_source(store.players, SOURCE)
    la_liga_by_season = _la_liga_teams_by_season(storage)

    # Se lee ``cup-events`` una sola vez: lo consumen tanto el calendario (fixtures)
    # como los minutos, y ``iter_latest`` reparsea el dataset entero en cada llamada.
    cup_events, cup_anomalies = _read_events(storage, CUP_EVENTS_DATASET)
    fixtures, league_anomalies = _build_fixtures(storage, la_liga_by_season, cup_events)
    minutes, lineup_anomalies = _build_cup_minutes(
        storage, la_liga_by_season, canonical_by_sofascore, cup_events
    )

    storage.curated.write_partitioned(FIXTURES_TABLE, fixtures, FIXTURES_COLUMNS)
    storage.curated.write_partitioned(CUP_MINUTES_TABLE, minutes, CUP_MINUTES_COLUMNS)

    anomalies: dict[str, int] = {}
    if cup_anomalies + league_anomalies:
        anomalies["eventos ilegibles"] = cup_anomalies + league_anomalies
    if lineup_anomalies:
        anomalies["lineups ilegibles"] = lineup_anomalies
    result = IngestResult(
        rows={FIXTURES_TABLE: len(fixtures), CUP_MINUTES_TABLE: len(minutes)},
        stats={"partidos_calendario": len({r["event_id"] for r in fixtures})},
        anomalies=anomalies,
    )
    logger.info(
        "sofascore copas re-cura: %d fixtures, %d filas de minutos",
        len(fixtures),
        len(minutes),
    )
    return result


def _la_liga_teams_by_season(storage: Storage) -> dict[str, set[int]]:
    """``temporada -> {team_id}`` de los equipos de La Liga, del catálogo ``sofascore_teams``.

    El catálogo etiqueta la temporada como año de inicio (2025), la misma clave que usa el
    backfill de copas. Si el catálogo no está construido, no hay "nuestros" y todo queda
    vacío (el runbook construye el catálogo antes que las copas).
    """
    try:
        teams = storage.curated.read_table("sofascore_teams")
    except (FileNotFoundError, OSError):
        return {}
    if teams.empty or "competition" not in teams.columns:
        return {}
    la_liga = teams[teams["competition"].astype(str) == "la-liga"]
    by_season: dict[str, set[int]] = defaultdict(set)
    for row in la_liga.itertuples():
        team_id = _as_int(row.team_id)
        if team_id is not None:
            by_season[str(row.season)].add(team_id)
    return by_season


def _read_events(storage: Storage, dataset: str) -> tuple[list[tuple], int]:
    """``[(evento, competición, temporada)]`` de los partidos terminados de un dataset.

    La competición es el slug conocido (La Liga, Segunda, copas); un torneo fuera de
    cobertura (rival extranjero en su propia liga) se salta. La temporada es el año de
    inicio como cadena. Devuelve también el número de ficheros ilegibles.
    """
    rows: list[tuple] = []
    anomalies = 0
    for _name, payload in storage.raw.iter_latest(SOURCE, dataset):
        try:
            response = EventsResponse.model_validate_json(payload)
        except ValidationError:
            anomalies += 1
            continue
        for event in response.events:
            if not event.finished:
                continue
            slug = COMPETITION_BY_ANY_TOURNAMENT.get(event.unique_tournament_id)
            year = season_start_year(event.season.year if event.season else None)
            if slug is None or year is None:
                continue
            rows.append((event, slug, str(year)))
    return rows, anomalies


def _build_fixtures(
    storage: Storage, la_liga_by_season: dict[str, set[int]], cup_events: list[tuple]
) -> tuple[list[dict], int]:
    """Un partido por fila, de liga (``tournament-events``) y copas (``cup-events``, ya leídas).

    Se queda con los partidos donde juega algún equipo de La Liga de esa temporada, de
    modo que la densidad de calendario de un equipo (días desde el último partido de
    cualquier competición, partido entre semana próximo) se resuelve con esta única tabla.
    """
    league, league_anomalies = _read_events(storage, "tournament-events")
    rows: dict[int, dict] = {}
    for event, competition, season in [*league, *cup_events]:
        la_liga = la_liga_by_season.get(season, set())
        if event.home_team.id not in la_liga and event.away_team.id not in la_liga:
            continue
        rows[event.id] = {
            "event_id": event.id,
            "date": _event_date(event),
            "home_team_id": event.home_team.id,
            "home_team_name": event.home_team.name,
            "away_team_id": event.away_team.id,
            "away_team_name": event.away_team.name,
            "competition": competition,
            "season": season,
        }
    return list(rows.values()), league_anomalies


def _build_cup_minutes(
    storage: Storage,
    la_liga_by_season: dict[str, set[int]],
    canonical_by_sofascore: dict[str, str],
    cup_events: list[tuple],
) -> tuple[list[dict], int]:
    """Minutos por jugador-partido de los equipos de La Liga en las copas, desde ``cup-lineups``."""
    meta = {event.id: (event, competition, season) for event, competition, season in cup_events}

    rows: list[dict] = []
    anomalies = 0
    for name, payload in storage.raw.iter_latest(SOURCE, CUP_LINEUPS_DATASET):
        try:
            event_id = int(name)
        except ValueError:
            continue
        if event_id not in meta:
            continue
        event, competition, season = meta[event_id]
        try:
            lineups = LineupsResponse.model_validate_json(payload)
        except ValidationError:
            anomalies += 1
            continue
        la_liga = la_liga_by_season.get(season, set())
        rows.extend(
            _cup_minute_rows(event, lineups, competition, season, la_liga, canonical_by_sofascore)
        )
    return rows, anomalies


def _cup_minute_rows(
    event: CalendarEvent,
    lineups: LineupsResponse,
    competition: str,
    season: str,
    la_liga_teams: set[int],
    canonical_by_sofascore: dict[str, str],
) -> list[dict]:
    """Una fila por jugador de un equipo de La Liga que jugó el partido de copa."""
    date = _event_date(event)
    rows: list[dict] = []
    for side, is_home in ((lineups.home, True), (lineups.away, False)):
        team = event.home_team if is_home else event.away_team
        if team.id not in la_liga_teams:
            continue  # el lado rival (extranjero o modesto) no nos interesa
        opponent = event.away_team if is_home else event.home_team
        for entry in side.players:
            player_id = entry.player.id
            if player_id is None or not entry.statistics:
                continue
            rows.append(
                {
                    "player_event": f"{player_id}_{event.id}",
                    "canonical_id": canonical_by_sofascore.get(str(player_id), ""),
                    "sofascore_player_id": player_id,
                    "event_id": event.id,
                    "date": date,
                    "team_id": team.id,
                    "opponent_id": opponent.id,
                    "is_home": is_home,
                    "is_starting": entry.substitute is False,
                    "minutes": _as_number(entry.statistics.get("minutesPlayed")),
                    "competition": competition,
                    "season": season,
                }
            )
    return rows


def _event_date(event: CalendarEvent) -> str | None:
    if event.start_timestamp is None:
        return None
    return datetime.fromtimestamp(event.start_timestamp, tz=UTC).date().isoformat()


def _as_int(value) -> int | None:
    if pd.isna(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
