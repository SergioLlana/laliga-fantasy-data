"""Ingesta de la plantilla de una competición a las tablas curadas."""

from __future__ import annotations

from datetime import date

import pandas as pd

from lfdata.sources.biwenger.client import PROXY_ENABLED, WAIT_SECONDS, BiwengerClient
from lfdata.sources.biwenger.models import PlayerDetail
from lfdata.sources.http import HttpTransport, scrapeops_proxy_from_env
from lfdata.storage import Storage

# Los cinco sistemas de puntuación de Biwenger, por id de sistema.
POINT_SYSTEMS = {
    "1": "points_as",
    "2": "points_sofascore",
    "3": "points_stats",
    "5": "points_media",
    "6": "points_social",
}
POINT_COLUMNS = list(POINT_SYSTEMS.values())


def ingest_squad(
    storage: Storage,
    competition: str,
    *,
    transport: HttpTransport | None = None,
) -> dict[str, int]:
    """Descarga la plantilla y publica biwenger_players y biwenger_teams.

    Devuelve el número de filas escritas por tabla.
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        proxy=scrapeops_proxy_from_env(enabled=PROXY_ENABLED),
    )
    client = BiwengerClient(transport, storage.raw)
    data = client.fetch_competition_data(competition).data

    nullable_ints = [
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
    players = pd.DataFrame([player.model_dump() for player in data.players.values()]).astype(
        dict.fromkeys(nullable_ints, "Int64")
    )
    teams = pd.DataFrame([team.model_dump() for team in data.teams.values()])

    partition = {"competition": competition}
    storage.curated.write_table("biwenger_players", players, partition=partition)
    storage.curated.write_table("biwenger_teams", teams, partition=partition)
    return {"biwenger_players": len(players), "biwenger_teams": len(teams)}


def _date_from_biwenger(stamp: int) -> date:
    """Convierte el entero AAMMDD de Biwenger (250721) en fecha (2025-07-21)."""
    return date(2000 + stamp // 10000, stamp // 100 % 100, stamp % 100)


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


def ingest_reports(
    storage: Storage,
    competition: str,
    season: str,
    *,
    transport: HttpTransport | None = None,
) -> dict[str, int]:
    """Publica fantasy_points y biwenger_prices de una competición y temporada.

    Recorre todos los jugadores de la plantilla y descarga su detalle. La
    escritura reemplaza la partición (competition, season) por completo, de modo
    que reejecutar el comando no duplica filas. Devuelve filas escritas por tabla.
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        proxy=scrapeops_proxy_from_env(enabled=PROXY_ENABLED),
    )
    client = BiwengerClient(transport, storage.raw)
    data = client.fetch_competition_data(competition).data

    points_rows: list[dict] = []
    price_rows: list[dict] = []
    for player in data.players.values():
        detail = client.fetch_player_reports(competition, player.slug, season).data
        points_rows.extend(_points_rows(detail))
        price_rows.extend(_price_rows(detail))

    nullable_ints = ["match_id", "round_id", "minutes", "home_score", "away_score", *POINT_COLUMNS]
    points = pd.DataFrame(
        points_rows,
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
    prices = pd.DataFrame(price_rows, columns=["player_id", "date", "price"])

    partition = {"competition": competition, "season": season}
    storage.curated.write_table("fantasy_points", points, partition=partition)
    storage.curated.write_table("biwenger_prices", prices, partition=partition)
    return {"fantasy_points": len(points), "biwenger_prices": len(prices)}
