"""Ingesta de Transfermarkt a las tablas curadas.

Recorre los clubes de la competición, sus plantillas y, por jugador, el perfil,
el histórico de valor, los traspasos, la disponibilidad partido a partido y el
historial de lesiones. Produce cinco tablas (aún con IDs de Transfermarkt; el
mapping a IDs canónicos es un paso posterior):

- ``transfermarkt_players``  jugador (perfil + pertenencia a plantilla)
- ``market_values_tm``       jugador-fecha (valor y club en esa fecha)
- ``transfers``              movimiento (cesión / fin de cesión / traspaso)
- ``availability_tm``        jugador-partido (disponibilidad, minutos, cambios)
- ``injuries_tm``            lesión (diagnóstico, fechas, días, partidos perdidos)
"""

from __future__ import annotations

import pandas as pd

from lfdata.sources.http import HttpTransport, scrapeops_proxy_from_env
from lfdata.sources.transfermarkt.client import PROXY_ENABLED, WAIT_SECONDS, TransfermarktClient
from lfdata.sources.transfermarkt.parse import (
    availability_rows,
    market_value_rows,
    transfer_rows,
)
from lfdata.storage import Storage

# Temporada por defecto: saison_id de Transfermarkt es el año de inicio
# (2025 = temporada 2025-26).
DEFAULT_SEASON = 2025


def ingest_squads(
    storage: Storage,
    competition: str,
    *,
    season: int = DEFAULT_SEASON,
    transport: HttpTransport | None = None,
    max_clubs: int | None = None,
) -> dict[str, int]:
    """Descarga la competición completa y publica las cinco tablas curadas.

    ``max_clubs`` limita el número de clubes recorridos (útil para una primera
    prueba real, dado que el recorrido completo son miles de peticiones a 4 s).
    Devuelve el número de filas escritas por tabla.
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        proxy=scrapeops_proxy_from_env(enabled=PROXY_ENABLED),
    )
    client = TransfermarktClient(transport, storage.raw)

    clubs = client.fetch_competition_clubs(competition, season=season)
    if max_clubs is not None:
        clubs = clubs[:max_clubs]

    player_records: list[dict] = []
    value_records: list[dict] = []
    transfer_records: list[dict] = []
    availability_records: list[dict] = []
    injury_records: list[dict] = []

    for club in clubs:
        for member in client.fetch_squad(club.id, season=season):
            player_id = member.player_id
            profile = client.fetch_player_profile(player_id, slug=member.slug)
            player_records.append(
                {
                    "id": player_id,
                    "slug": member.slug,
                    "name": profile.name or member.name,
                    "birth_date": profile.birth_date,
                    "position": profile.position or member.position,
                    "shirt_number": member.shirt_number,
                    "club_id": club.id,
                    "club_name": club.name,
                }
            )
            value_records += market_value_rows(
                client.fetch_market_value(player_id), player_id=player_id
            )
            transfer_records += transfer_rows(
                client.fetch_transfers(player_id), player_id=player_id
            )
            availability_records += availability_rows(
                client.fetch_performance(player_id), player_id=player_id
            )
            injury_records += [
                _injury_record(injury)
                for injury in client.fetch_injuries(player_id, slug=member.slug)
            ]

    frames = {
        "transfermarkt_players": _players_frame(player_records),
        "market_values_tm": _values_frame(value_records),
        "transfers": _transfers_frame(transfer_records),
        "availability_tm": _availability_frame(availability_records),
        "injuries_tm": _injuries_frame(injury_records),
    }
    partition = {"competition": competition}
    for table, frame in frames.items():
        storage.curated.write_table(table, frame, partition=partition)
    return {table: len(frame) for table, frame in frames.items()}


def _players_frame(records: list[dict]) -> pd.DataFrame:
    columns = [
        "id",
        "slug",
        "name",
        "birth_date",
        "position",
        "shirt_number",
        "club_id",
        "club_name",
    ]
    df = pd.DataFrame(records, columns=columns)
    df["birth_date"] = pd.to_datetime(df["birth_date"], errors="coerce")
    return df.astype({"shirt_number": "Int64"})


def _values_frame(records: list[dict]) -> pd.DataFrame:
    columns = ["player_id", "date", "value", "club_name"]
    df = pd.DataFrame(records, columns=columns)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.astype({"value": "Int64"})


def _transfers_frame(records: list[dict]) -> pd.DataFrame:
    columns = [
        "player_id",
        "date",
        "season",
        "type",
        "fee",
        "market_value",
        "from_club_id",
        "from_club_name",
        "to_club_id",
        "to_club_name",
    ]
    df = pd.DataFrame(records, columns=columns)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.astype({"from_club_id": "Int64", "to_club_id": "Int64"})


def _availability_frame(records: list[dict]) -> pd.DataFrame:
    columns = [
        "player_id",
        "game_id",
        "date",
        "competition_id",
        "season_id",
        "game_day",
        "club_id",
        "opponent_club_id",
        "participation_state",
        "played_minutes",
        "is_starting",
        "substituted_in_minute",
        "substituted_out_minute",
        "injury_id",
        "absence_id",
    ]
    df = pd.DataFrame(records, columns=columns)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    nullable_ints = [
        "season_id",
        "game_day",
        "club_id",
        "opponent_club_id",
        "played_minutes",
        "substituted_in_minute",
        "substituted_out_minute",
        "injury_id",
        "absence_id",
    ]
    return df.astype(dict.fromkeys(nullable_ints, "Int64"))


def _injury_record(injury) -> dict:
    return {
        "player_id": injury.player_id,
        "season": injury.season,
        "injury": injury.injury,
        "from_date": injury.from_date,
        "until_date": injury.until_date,
        "days": injury.days,
        "games_missed": injury.games_missed,
    }


def _injuries_frame(records: list[dict]) -> pd.DataFrame:
    columns = [
        "player_id",
        "season",
        "injury",
        "from_date",
        "until_date",
        "days",
        "games_missed",
    ]
    df = pd.DataFrame(records, columns=columns)
    df["from_date"] = pd.to_datetime(df["from_date"], errors="coerce")
    df["until_date"] = pd.to_datetime(df["until_date"], errors="coerce")
    return df.astype({"days": "Int64", "games_missed": "Int64"})
