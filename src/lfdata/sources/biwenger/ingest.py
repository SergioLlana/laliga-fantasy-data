"""Ingesta de la plantilla de una competición a las tablas curadas."""

from __future__ import annotations

import pandas as pd

from lfdata.sources.biwenger.client import PROXY_ENABLED, WAIT_SECONDS, BiwengerClient
from lfdata.sources.http import HttpTransport, scrapeops_proxy_from_env
from lfdata.storage import Storage


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
