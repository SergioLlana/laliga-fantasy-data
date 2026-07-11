"""Ingesta bajo demanda de SofaScore: historial completo de un jugador.

``ingest_player`` resuelve un jugador (por canonical_id, id de SofaScore o
nombre), recorre **todas** sus temporadas en cualquier liga y publica dos tablas
curadas:

- ``player_season_stats`` — grano jugador-temporada: el agregado de 115 campos.
- ``player_match_stats`` — grano jugador-partido: nota y métricas de evento
  (minutos, pases, remates, goles, asistencias, xG...), una petición por partido.

Cada fila lleva ``canonical_id`` si el id de SofaScore ya está mapeado (ADR 0001);
si no, la fila conserva solo el ``sofascore_player_id`` y el jugador se encola en
``mappings/sofascore-review.csv`` para una ronda de matching posterior.

Es el modo más simple del paso 3 (docs/implementation/03) y desbloquea el
baseline de fichajes (paso 5): dado un fichaje de otra liga, un comando trae su
historial completo.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from lfdata.mappings import MappingStore
from lfdata.sources.http import HttpTransport, SourceHTTPError, scrapeops_proxy_from_env
from lfdata.sources.ingestion import IngestResult, PlayerFailure
from lfdata.sources.sofascore.client import PROXY_OVERFLOW, WAIT_SECONDS, SofaScoreClient
from lfdata.sources.sofascore.models import SeasonRating
from lfdata.storage import Storage

logger = logging.getLogger(__name__)

SOURCE = "sofascore"
_CANONICAL_RE = re.compile(r"^p\d+$")

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

REVIEW_COLUMNS = ["sofascore_id", "sofascore_name", "sofascore_team", "sofascore_dob", "decision"]


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

    player_id, name, team, dob = _resolve_player(client, query, store)
    canonical_id = canonical_by_sofascore.get(str(player_id), "")
    if not canonical_id:
        _enqueue_review(Path(mappings_dir), player_id, name, team, dob)

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


def _resolve_player(
    client: SofaScoreClient, query: str, store: MappingStore
) -> tuple[int, str | None, str | None, str | None]:
    """Resuelve ``query`` a (id SofaScore, nombre, club, fecha nacimiento ISO).

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
        return int(rows.iloc[0]["id_en_fuente"]), None, None, None

    if query.isdigit():
        return int(query), None, None, None

    players = client.search_players(query).players()
    if not players:
        raise ValueError(f"SofaScore no devolvió ningún jugador para {query!r}.")
    best = players[0]
    dob = (
        datetime.fromtimestamp(best.date_of_birth_timestamp, tz=UTC).date().isoformat()
        if best.date_of_birth_timestamp is not None
        else None
    )
    return best.id, best.name, (best.team.name if best.team else None), dob


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


def _match_row(
    rating: SeasonRating, stats: dict | None, player_id: int, common: dict
) -> dict:
    """Fila de player_match_stats: contexto del partido + métricas de evento."""
    opponent = rating.opponent
    date = (
        datetime.fromtimestamp(rating.start_timestamp, tz=UTC).date().isoformat()
        if rating.start_timestamp is not None
        else None
    )
    row = {
        _MATCH_KEY: f"{player_id}_{rating.event_id}",
        **common,
        "event_id": rating.event_id,
        "date": date,
        "opponent": opponent.name if opponent else None,
        "opponent_id": opponent.id if opponent else None,
        "is_home": rating.is_home,
        "rating": rating.rating,
    }
    # Sin detalle de evento (fallo puntual): métricas nulas, no cero. Con detalle,
    # la ausencia de un conteo es cero (SofaScore omite los ceros).
    for neutral, key in _MATCH_COUNT_FIELDS.items():
        row[neutral] = None if stats is None else _as_number(stats.get(key, 0))
    for neutral, key in _MATCH_FLOAT_FIELDS.items():
        row[neutral] = None if stats is None else _as_number(stats.get(key))
    return row


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


def _enqueue_review(
    mappings_dir: Path, player_id: int, name: str | None, team: str | None, dob: str | None
) -> None:
    """Añade el jugador sin mapping a mappings/sofascore-review.csv (idempotente).

    El fichero es la cola para la ronda de matching de IDs de SofaScore a
    canónicos (paso 3, orden de trabajo 5). Si el id ya está encolado no se
    duplica; el trabajo manual posterior rellena ``decision``.
    """
    path = mappings_dir / "sofascore-review.csv"
    if path.exists():
        review = pd.read_csv(path, dtype=str, keep_default_na=False)
        for col in REVIEW_COLUMNS:
            if col not in review.columns:
                review[col] = ""
        review = review[REVIEW_COLUMNS]
    else:
        review = pd.DataFrame(columns=REVIEW_COLUMNS)
    if str(player_id) in set(review["sofascore_id"]):
        return
    new_row = {
        "sofascore_id": str(player_id),
        "sofascore_name": name or "",
        "sofascore_team": team or "",
        "sofascore_dob": dob or "",
        "decision": "",
    }
    review = pd.concat([review, pd.DataFrame([new_row])], ignore_index=True)
    mappings_dir.mkdir(parents=True, exist_ok=True)
    review.to_csv(path, index=False)
