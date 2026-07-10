"""Ingesta de la plantilla de una competición a las tablas curadas."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import date

import pandas as pd

from lfdata.sources.biwenger.client import PROXY_OVERFLOW, WAIT_SECONDS, BiwengerClient
from lfdata.sources.biwenger.models import Player, PlayerDetail
from lfdata.sources.http import HttpTransport, SourceHTTPError, scrapeops_proxy_from_env
from lfdata.sources.ingestion import IngestResult, PlayerFailure
from lfdata.storage import Storage

logger = logging.getLogger(__name__)

# Los cinco sistemas de puntuación de Biwenger, por id de sistema.
POINT_SYSTEMS = {
    "1": "points_as",
    "2": "points_sofascore",
    "3": "points_stats",
    "5": "points_media",
    "6": "points_social",
}
POINT_COLUMNS = list(POINT_SYSTEMS.values())

# Motivo de anomalía: reports que Biwenger sirve con puntos pero sin rawStats.
POINTS_WITHOUT_STATS = "reports con puntos sin rawStats"

# Los reports se escriben por lotes de este tamaño (en jugadores): un fallo tras
# el jugador N conserva en curated todo lote ya volcado, en vez de tirar el run.
REPORTS_BATCH_SIZE = 25

_PLAYER_NULLABLE_INTS = [
    "team_id",
    "number",
    "fantasy_price",
    "points",
    "points_home",
    "points_away",
    "played_home",
    "played_away",
    "points_last_season",
]


def _players_frame(
    players: Iterable[Player], birth_dates: Mapping[int, str | None] | None = None
) -> pd.DataFrame:
    """DataFrame de biwenger_players con ``birth_date`` (ISO) por id, o vacía.

    La plantilla no trae fecha de nacimiento; la aporta la ingesta de reports
    desde el detalle por jugador (``birth_dates``). Sin ese dato la columna queda
    ausente y el matcher la trata como faltante.
    """
    births = birth_dates or {}
    records = []
    for player in players:
        record = player.model_dump()
        record["birth_date"] = births.get(player.id)
        records.append(record)
    return pd.DataFrame(records).astype(dict.fromkeys(_PLAYER_NULLABLE_INTS, "Int64"))


def ingest_squad(
    storage: Storage,
    competition: str,
    *,
    transport: HttpTransport | None = None,
) -> IngestResult:
    """Descarga la plantilla y publica biwenger_players y biwenger_teams.

    Devuelve el número de filas escritas por tabla.
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )
    client = BiwengerClient(transport, storage.raw)
    data = client.fetch_competition_data(competition).data

    players = _players_frame(data.players.values())
    teams = pd.DataFrame([team.model_dump() for team in data.teams.values()])

    partition = {"competition": competition}
    storage.curated.write_table("biwenger_players", players, partition=partition)
    storage.curated.write_table("biwenger_teams", teams, partition=partition)
    logger.info(
        "biwenger plantilla %s: %d jugadores, %d equipos",
        competition,
        len(players),
        len(teams),
    )
    return IngestResult(rows={"biwenger_players": len(players), "biwenger_teams": len(teams)})


def _date_from_biwenger(stamp: int) -> date:
    """Convierte el entero AAMMDD de Biwenger (250721) en fecha (2025-07-21)."""
    return date(2000 + stamp // 10000, stamp // 100 % 100, stamp % 100)


def _birthday_to_iso(stamp: int | None) -> str | None:
    """Convierte el entero AAAAMMDD del detalle (20010412) en ISO (2001-04-12).

    Distinto del AAMMDD de los precios: la fecha de nacimiento trae el año con
    cuatro cifras. Devuelve ``None`` si el detalle no la publica: puede faltar
    (``None``) o venir como ``0`` (Biwenger lo usa para "fecha desconocida").
    """
    if not stamp:
        return None
    return date(stamp // 10000, stamp // 100 % 100, stamp % 100).isoformat()


def _points_without_stats(detail: PlayerDetail) -> int:
    """Cuántos reports del jugador traen puntos pero les falta ``rawStats``."""
    return sum(report.points_without_stats for report in detail.reports)


def _points_rows(detail: PlayerDetail) -> list[dict]:
    """Una fila por partido puntuado: los 5 sistemas, minutos, nota y resultado."""
    rows = []
    for report in detail.reports:
        if not report.scored:
            continue
        stats = report.raw_stats
        result = "win" if stats.win else "loss" if stats.lost else "draw"
        row = {
            "player_id": detail.id,
            "match_id": report.match.id,
            "round_id": report.match.round.id,
            "home": report.home,
            "minutes": stats.minutes_played,
            "sofascore_grade": stats.sofascore,
            "home_score": stats.home_score,
            "away_score": stats.away_score,
            "result": result,
        }
        row.update({column: report.points.get(system) for system, column in POINT_SYSTEMS.items()})
        rows.append(row)
    return rows


def _price_rows(detail: PlayerDetail) -> list[dict]:
    """Una fila por día con precio."""
    return [
        {"player_id": detail.id, "date": _date_from_biwenger(stamp), "price": price}
        for stamp, price in detail.prices
    ]


def _points_frame(rows: list[dict]) -> pd.DataFrame:
    nullable_ints = ["match_id", "round_id", "minutes", "home_score", "away_score", *POINT_COLUMNS]
    return pd.DataFrame(
        rows,
        columns=[
            "player_id",
            "match_id",
            "round_id",
            *POINT_COLUMNS,
            "minutes",
            "sofascore_grade",
            "home",
            "home_score",
            "away_score",
            "result",
        ],
    ).astype(dict.fromkeys(nullable_ints, "Int64"))


def _prices_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["player_id", "date", "price"])


def ingest_reports(
    storage: Storage,
    competition: str,
    season: str,
    *,
    transport: HttpTransport | None = None,
    batch_size: int = REPORTS_BATCH_SIZE,
) -> IngestResult:
    """Publica fantasy_points y biwenger_prices de una competición y temporada.

    Recorre todos los jugadores de la plantilla y descarga su detalle. Las filas
    se vuelcan por lotes con ``upsert_table`` (clave ``player_id``) durante el
    recorrido, de modo que un fallo a mitad conserva en curated todo lo ya
    descargado y reejecutar no duplica. Un jugador que la fuente ya no sirve (404)
    se registra como fallo y se salta sin abortar el run. Devuelve las filas
    escritas por tabla y los jugadores fallidos.

    El detalle trae la fecha de nacimiento (``birthday``), que la plantilla no
    publica: se refresca en ``biwenger_players`` (upsert por ``id``, misma
    partición de competición) para endurecer el matching de identidad (#37).
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )
    client = BiwengerClient(transport, storage.raw)
    data = client.fetch_competition_data(competition).data

    partition = {"competition": competition, "season": season}
    result = IngestResult(rows={"fantasy_points": 0, "biwenger_prices": 0})
    points_without_stats = 0
    total = len(data.players)
    batch_points: list[dict] = []
    batch_prices: list[dict] = []
    batch_players: list[Player] = []
    births: dict[int, str | None] = {}

    def flush() -> None:
        if batch_players:
            storage.curated.upsert_table(
                "biwenger_players",
                _players_frame(batch_players, births),
                key="id",
                partition={"competition": competition},
            )
        if not batch_points and not batch_prices:
            batch_players.clear()
            return
        storage.curated.upsert_table(
            "fantasy_points", _points_frame(batch_points), key="player_id", partition=partition
        )
        storage.curated.upsert_table(
            "biwenger_prices", _prices_frame(batch_prices), key="player_id", partition=partition
        )
        result.rows["fantasy_points"] += len(batch_points)
        result.rows["biwenger_prices"] += len(batch_prices)
        batch_points.clear()
        batch_prices.clear()
        batch_players.clear()

    for index, player in enumerate(data.players.values(), start=1):
        try:
            detail = client.fetch_player_reports(competition, player.slug, season).data
        except SourceHTTPError as error:
            result.failures.append(PlayerFailure(player.slug, error.url, error.status))
            logger.warning(
                "biwenger %s [%d/%d] %s: HTTP %d, saltado",
                competition,
                index,
                total,
                player.slug,
                error.status,
            )
            continue
        batch_points.extend(_points_rows(detail))
        batch_prices.extend(_price_rows(detail))
        batch_players.append(player)
        births[player.id] = _birthday_to_iso(detail.birthday)
        incomplete = _points_without_stats(detail)
        if incomplete:
            points_without_stats += incomplete
            logger.warning(
                "biwenger %s [%d/%d] %s: %d reports con puntos pero sin rawStats, sin curar",
                competition,
                index,
                total,
                player.slug,
                incomplete,
            )
        logger.info("biwenger %s [%d/%d] %s", competition, index, total, player.slug)
        if index % batch_size == 0:
            flush()
    flush()

    if points_without_stats:
        result.anomalies[POINTS_WITHOUT_STATS] = points_without_stats

    logger.info(
        "biwenger reports %s %s: %d filas de puntos, %d de precios, %d fallidos, "
        "%d reports con puntos sin rawStats",
        competition,
        season,
        result.rows["fantasy_points"],
        result.rows["biwenger_prices"],
        len(result.failures),
        points_without_stats,
    )
    return result
