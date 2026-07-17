"""Catálogo de identidad de SofaScore, reconstruido desde raw/ sin peticiones.

El matcher de identidad (``lfdata map``) necesita, de cada jugador de SofaScore,
la evidencia con la que se compara contra Biwenger: nombre, **fecha de
nacimiento** y club. Ni ``search/all`` la trae completa (sin fecha, con el club
potencialmente desfasado) ni ``player_match_stats`` la conserva. La fecha solo
viene entera en las alineaciones, ya descargadas por el backfill. Por eso se
publica como dos tablas curadas nuevas, construidas **solo desde raw/**
(``event-lineups`` + ``tournament-events``), sin volver a pedir nada (ADR 0003):

- ``sofascore_players`` — por (jugador, competición, temporada): id, nombre,
  fecha de nacimiento, equipo. Es la evidencia del matcher de jugadores.
- ``sofascore_teams`` — por (equipo, competición, temporada): id y nombre. Es la
  evidencia del matcher de equipos, que tiene que resolverse **antes** que el de
  jugadores (los jugadores se buscan dentro del club ya mapeado).

El cuerpo de un lineup no trae ni el nombre del equipo ni la competición/temporada
del partido, solo ids numéricos; esos datos salen de ``tournament-events`` (que el
backfill ya descarga) y se cruzan por ``event_id`` (el nombre del fichero raw del
lineup). El equipo de cada jugador se toma del **lado** (home/away) en que aparece
en la alineación, no de su ``teamId`` por jugador —que en los datos reales trae
ruido (cedidos, registros paralelos)—: el lado es inequívoco.

La cobertura del catálogo es exactamente lo backfilleado (La Liga/Segunda): el modo
bajo demanda no descarga alineaciones, así que un fichaje de otra liga no aparece
aquí (lo cubre el camino bajo demanda, issue #81).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from lfdata.mappings import MappingStore
from lfdata.sources.ingestion import IngestResult
from lfdata.sources.sofascore.client import COMPETITION_BY_TOURNAMENT
from lfdata.sources.sofascore.ingest import season_start_year
from lfdata.sources.sofascore.models import EventsResponse, LineupsResponse
from lfdata.storage import Storage

logger = logging.getLogger(__name__)

SOURCE = "sofascore"

PLAYER_COLUMNS = [
    "sofascore_player_id",
    "name",
    "birth_date",
    "team_id",
    "team_name",
    "competition",
    "season",
]
TEAM_COLUMNS = ["team_id", "team_name", "competition", "season"]


def build_catalog(storage: Storage) -> IngestResult:
    """Reconstruye ``sofascore_players`` y ``sofascore_teams`` desde raw/.

    No hace ninguna petición: recorre todo el raw de ``tournament-events`` y
    ``event-lineups`` y reescribe las particiones ``(competición, temporada)``
    afectadas. La temporada se guarda como el **año de inicio** del proyecto (2025),
    no el id opaco de SofaScore, para que el matcher pueda compararla con
    ``transfermarkt_players`` (``--season`` = año de inicio en todas las fuentes).
    """
    events, teams, anomalies = _read_events(storage)
    players, player_anomalies = _read_lineups(storage, events)

    team_rows = [
        {"team_id": team_id, "team_name": name, "competition": comp, "season": season}
        for (comp, season, team_id), name in sorted(teams.items())
    ]
    _write_partitioned(storage, "sofascore_teams", team_rows, TEAM_COLUMNS)
    _write_partitioned(storage, "sofascore_players", list(players.values()), PLAYER_COLUMNS)

    return IngestResult(
        rows={"sofascore_players": len(players), "sofascore_teams": len(team_rows)},
        stats={"eventos": len(events)},
        anomalies={"tournament-events ilegible": anomalies, "lineups ilegible": player_anomalies}
        if anomalies or player_anomalies
        else {},
    )


def _read_events(storage: Storage) -> tuple[dict[int, dict], dict[tuple, str], int]:
    """De ``tournament-events``: metadatos por evento y catálogo de equipos.

    Devuelve ``(events, teams, anomalías)`` donde ``events`` mapea
    ``event_id -> {competition, season, home, away}`` y ``teams`` mapea
    ``(competition, season, team_id) -> team_name``.
    """
    events: dict[int, dict] = {}
    teams: dict[tuple, str] = {}
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
            year = season_start_year(event.season.year if event.season else None)
            if ut_id is None or year is None:
                continue
            # Slug conocido para La Liga/Segunda; una liga fuera de cobertura
            # (otro torneo) conserva su id opaco y simplemente no cruza con Biwenger.
            competition = COMPETITION_BY_TOURNAMENT.get(ut_id, str(ut_id))
            season = str(year)
            events[event.id] = {
                "competition": competition,
                "season": season,
                "home": (event.home_team.id, event.home_team.name),
                "away": (event.away_team.id, event.away_team.name),
            }
            for team_id, team_name in (events[event.id]["home"], events[event.id]["away"]):
                teams[(competition, season, team_id)] = team_name
    return events, teams, anomalies


def _read_lineups(storage: Storage, events: dict[int, dict]) -> tuple[dict[tuple, dict], int]:
    """De ``event-lineups``: una fila por (jugador, competición, temporada, equipo).

    El equipo sale del lado (home/away) en que juega, cruzado con los metadatos del
    evento (por ``event_id`` = nombre del fichero raw). Un lineup cuyo evento no está
    en ``tournament-events`` se salta: sin él no hay competición, temporada ni equipo.
    """
    players: dict[tuple, dict] = {}
    anomalies = 0
    for name, payload in storage.raw.iter_latest(SOURCE, "event-lineups"):
        try:
            event_id = int(name)
        except ValueError:
            continue
        meta = events.get(event_id)
        if meta is None:
            continue
        try:
            lineups = LineupsResponse.model_validate_json(payload)
        except ValidationError:
            logger.warning("sofascore lineups ilegible: %s, se salta", name)
            anomalies += 1
            continue
        for side, which in ((lineups.home, "home"), (lineups.away, "away")):
            team_id, team_name = meta[which]
            for entry in side.players:
                player_id = entry.player.id
                if player_id is None:
                    continue
                key = (meta["competition"], meta["season"], player_id, team_id)
                players[key] = {
                    "sofascore_player_id": player_id,
                    "name": entry.player.name or "",
                    "birth_date": _birth_date(entry.player.date_of_birth_timestamp),
                    "team_id": team_id,
                    "team_name": team_name,
                    "competition": meta["competition"],
                    "season": meta["season"],
                }
    return players, anomalies


def _write_partitioned(storage: Storage, table: str, rows: list[dict], columns: list[str]) -> None:
    """Reescribe cada partición ``(competición, temporada)`` desde cero.

    El catálogo se reconstruye entero desde raw/, así que ``write_table`` (refresh
    completo de la partición) es lo correcto: lo que ya no está en raw desaparece.
    """
    by_partition: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        by_partition[(row["competition"], row["season"])].append(row)
    for (competition, season), part_rows in by_partition.items():
        storage.curated.write_table(
            table,
            pd.DataFrame(part_rows, columns=columns),
            partition={"competition": competition, "season": season},
        )


def _birth_date(timestamp: int | None) -> str:
    """Epoch UTC → fecha ISO ``YYYY-MM-DD``; vacío si no hay."""
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, tz=UTC).date().isoformat()


# --- re-estampado del canonical_id en las tablas ya curadas ------------------

_CANONICAL_TABLES = ("player_match_stats", "player_season_stats")


def restamp_canonical(storage: Storage, mappings_dir: str = "mappings") -> IngestResult:
    """Rellena ``canonical_id`` en ``player_match_stats`` y ``player_season_stats``.

    Tras una ronda de ``lfdata map``, las filas ya curadas siguen con el
    ``sofascore_player_id`` pero sin ``canonical_id``. Esto **no** re-descarga ni
    relee ``raw/``: cruza la tabla curada con los mappings aprobados y reescribe la
    partición cambiando solo esa columna de join. La re-cura de verdad desde raw/
    —que rehace la fila entera y recoge cualquier cambio de la lógica de curado— es
    :func:`~lfdata.sources.sofascore.ingest.rebuild_matches`. Es idempotente: sin
    cambios de mapping, deja las tablas igual.
    """
    store = MappingStore(Path(mappings_dir))
    store.load()
    canonical_by_sofascore = MappingStore.canonical_by_source(store.players, SOURCE)

    result = IngestResult(rows={}, stats={})
    for table in _CANONICAL_TABLES:
        result.rows[table] = _restamp_table(storage, table, canonical_by_sofascore)
    return result


def _restamp_table(storage: Storage, table: str, canonical_by_sofascore: dict[str, str]) -> int:
    """Reescribe cada partición de ``table`` con el ``canonical_id`` al día.

    Devuelve cuántas filas se reescribieron; 0 si la tabla no existe o no lleva
    ``sofascore_player_id``.
    """
    try:
        df = storage.curated.read_table(table)
    except (FileNotFoundError, OSError):
        return 0
    if df.empty or "sofascore_player_id" not in df.columns or "competition" not in df.columns:
        return 0

    df = df.copy()
    old = df["canonical_id"].fillna("").astype(str)
    df["canonical_id"] = [
        canonical_by_sofascore.get(_source_id(value), "") for value in df["sofascore_player_id"]
    ]
    # Marca qué filas cambian para no reescribir particiones idénticas: el
    # re-estampado corre tras cada `map` y a menudo no cambia nada (idempotente),
    # y estas son las tablas más grandes del repo.
    changed = df["canonical_id"] != old
    written = 0
    for (competition, season), part in df.groupby(["competition", "season"], sort=False):
        if not changed.loc[part.index].any():
            continue
        storage.curated.write_table(
            table, part, partition={"competition": str(competition), "season": str(season)}
        )
        written += len(part)
    return written


def _source_id(value) -> str:
    """``sofascore_player_id`` (int/float/str en Parquet) a la clave del mapping."""
    if pd.isna(value):
        return ""
    if isinstance(value, float):
        return str(int(value))
    return str(value)
