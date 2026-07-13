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

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import pandas as pd

from lfdata.sources.http import HttpTransport, SourceHTTPError, scrapeops_proxy_from_env
from lfdata.sources.ingestion import IngestResult, PlayerFailure
from lfdata.sources.transfermarkt.client import PROXY_OVERFLOW, WAIT_SECONDS, TransfermarktClient
from lfdata.sources.transfermarkt.parse import (
    Club,
    availability_rows,
    market_value_rows,
    transfer_rows,
)
from lfdata.storage import Storage

logger = logging.getLogger(__name__)

# Temporada por defecto: saison_id de Transfermarkt es el año de inicio
# (2026 = temporada 2026-27), la que está en curso.
DEFAULT_SEASON = 2026

# Cada tabla es un snapshot de historia completa por jugador; el upsert por club
# la actualiza clave a clave. ``transfermarkt_players`` se indexa por ``id``.
_TABLE_KEYS = {
    "transfermarkt_players": "id",
    "market_values_tm": "player_id",
    "transfers": "player_id",
    "availability_tm": "player_id",
    "injuries_tm": "player_id",
}


def ingest_squads(
    storage: Storage,
    competition: str,
    *,
    season: int = DEFAULT_SEASON,
    transport: HttpTransport | None = None,
    max_clubs: int | None = None,
    since_days: int | None = None,
) -> IngestResult:
    """Descarga la competición y publica las cinco tablas curadas, club a club.

    Cada club se escribe con ``upsert_table`` en cuanto se termina de recorrer,
    de modo que un run que falla a mitad conserva el progreso de los clubes ya
    escritos. Correr la competición entera equivale a un refresh completo; un run
    parcial (``max_clubs``) refresca solo esos jugadores sin tocar al resto.

    Un jugador que la fuente ya no sirve (p. ej. 404 de una baja) se registra
    como fallo y se salta: sigue contando como visto en la plantilla, así que no
    se le retira. Si la petición de una plantilla entera falla (p. ej. 502/504
    transitorio), el club se salta igual —se registra como fallo y el run
    continúa con el resto—. Un refresh completo (sin ``max_clubs``) retira de
    ``transfermarkt_players`` a quien ya no aparezca en ninguna plantilla, pero
    omite esa retirada si alguna plantilla falló: sus jugadores no se vieron y
    borrarlos sería confundir un fallo transitorio con una baja real.

    ``max_clubs`` limita el número de clubes recorridos (útil para una primera
    prueba real, dado que el recorrido completo son miles de peticiones a 4 s).

    ``since_days`` evita re-pedir a la fuente al jugador cuya descarga en ``raw/``
    sea más reciente que ese número de días: se le vuelve a curar igualmente,
    parseando el raw que ya tenemos. Saltarse también el curado (como se hacía
    antes) abría un agujero permanente en ``transfermarkt_players``: el jugador
    que hubiera desaparecido de la tabla no volvía a entrar nunca, porque su raw
    reciente hacía que se le siguiera saltando. La capa curada se reconstruye
    siempre desde raw (ADR 0003); raw es lo que no se re-descarga.

    Devuelve las filas escritas por tabla y los jugadores fallidos.
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )
    client = TransfermarktClient(transport, storage.raw)

    clubs = client.fetch_competition_clubs(competition, season=season)
    full_refresh = max_clubs is None
    if max_clubs is not None:
        clubs = clubs[:max_clubs]

    squad_partition = {"competition": competition, "season": str(season)}
    result, seen_player_ids, squad_failures = _ingest_clubs(
        storage, client, competition, clubs, season=season, since_days=since_days
    )

    if full_refresh and squad_failures:
        # Alguna plantilla no se pudo leer: sus jugadores no están en
        # ``seen_player_ids``, así que retirar ahora los borraría por un fallo
        # transitorio, no por una baja real. Se omite la poda hasta un refresh
        # que recorra la competición entera sin fallos de plantilla.
        logger.warning(
            "transfermarkt %s: %d plantillas fallaron; se omite la retirada de "
            "jugadores para no borrar a nadie por un fallo transitorio",
            competition,
            squad_failures,
        )
    elif full_refresh:
        # La poda es dentro de la temporada: retira a quien ya no está en ninguna
        # plantilla *de esta temporada*, sin tocar las demás.
        removed = storage.curated.retain_keys(
            "transfermarkt_players", seen_player_ids, key="id", partition=squad_partition
        )
        if removed:
            logger.info(
                "transfermarkt %s %d: %d jugadores retirados (ya no en ninguna plantilla)",
                competition,
                season,
                removed,
            )

    logger.info(
        "transfermarkt %s: %d jugadores curados, %d fallidos",
        competition,
        result.rows["transfermarkt_players"],
        len(result.failures),
    )
    return result


def ingest_clubs(
    storage: Storage,
    competition: str,
    club_ids: Iterable[int],
    *,
    season: int = DEFAULT_SEASON,
    transport: HttpTransport | None = None,
    since_days: int | None = None,
) -> IngestResult:
    """Refresh dirigido: solo las plantillas de ``club_ids``, sin poda.

    Lo que el detector de jugador nuevo necesita: cuando un fichaje aparece en la
    plantilla de Biwenger, su contraparte de Transfermarkt está en el kader de su
    club de llegada, y recorrer la competición entera (miles de peticiones) para
    llegar a un club es desproporcionado.

    Nunca retira a nadie: solo se han visto los jugadores de esos clubes, así que
    la poda —que es lo que da sentido a un refresh completo— no aplica aquí.
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )
    client = TransfermarktClient(transport, storage.raw)

    wanted = set(club_ids)
    clubs = [
        c for c in client.fetch_competition_clubs(competition, season=season) if c.id in wanted
    ]
    missing = wanted - {club.id for club in clubs}
    if missing:
        logger.warning(
            "transfermarkt %s %d: los clubes %s no están en la competición esa temporada",
            competition,
            season,
            sorted(missing),
        )

    result, _, _ = _ingest_clubs(
        storage, client, competition, clubs, season=season, since_days=since_days
    )
    logger.info(
        "transfermarkt %s %d: %d clubes refrescados, %d jugadores curados, %d fallidos",
        competition,
        season,
        len(clubs),
        result.rows["transfermarkt_players"],
        len(result.failures),
    )
    return result


def _ingest_clubs(
    storage: Storage,
    client: TransfermarktClient,
    competition: str,
    clubs: list[Club],
    *,
    season: int,
    since_days: int | None,
) -> tuple[IngestResult, set[int], int]:
    """Recorre los clubes dados y vuelca sus cinco tablas, club a club.

    Devuelve el resultado, los ids de jugador vistos en plantilla (los que un
    refresh completo puede podar) y cuántas plantillas fallaron. Es el núcleo
    compartido por el recorrido de la competición (:func:`ingest_squads`) y el
    refresh dirigido (:func:`ingest_clubs`); la única diferencia entre ambos es
    qué clubes llegan aquí y si después se poda.
    """
    # ``transfermarkt_players`` es la pertenencia a una plantilla, así que se
    # particiona también por temporada: ingerir 2023 no puede podar a los
    # jugadores de 2026. Las otras cuatro tablas son el histórico del jugador
    # (valores, traspasos, disponibilidad, lesiones), el mismo sea cual sea la
    # temporada desde la que se le alcance: van a la partición de competición.
    partition = {"competition": competition}
    squad_partition = {"competition": competition, "season": str(season)}
    result = IngestResult(rows=dict.fromkeys(_TABLE_KEYS, 0))
    seen_player_ids: set[int] = set()
    squad_failures = 0

    for club_index, club in enumerate(clubs, start=1):
        logger.info(
            "transfermarkt %s [%d/%d] club %s (%d)",
            competition,
            club_index,
            len(clubs),
            club.name,
            club.id,
        )
        try:
            # La plantilla completa es una sola petición; si falla no conocemos a
            # sus jugadores. Un 502/504 transitorio de la fuente aborta este club,
            # pero no el run: los ya escritos se conservan y el resto se sigue
            # recorriendo. Se registra como fallo para el resumen y el exit code.
            squad = client.fetch_squad(club.id, season=season)
        except SourceHTTPError as error:
            squad_failures += 1
            result.failures.append(PlayerFailure(f"club {club.name}", error.url, error.status))
            logger.warning(
                "transfermarkt %s club %s (%d): HTTP %d al pedir la plantilla, club saltado",
                competition,
                club.name,
                club.id,
                error.status,
            )
            continue

        player_records: list[dict] = []
        value_records: list[dict] = []
        transfer_records: list[dict] = []
        availability_records: list[dict] = []
        injury_records: list[dict] = []
        downloaded = reused = failed = 0

        for member in squad:
            player_id = member.player_id
            seen_player_ids.add(player_id)  # visto en plantilla aunque falle
            # Ya bajado hace poco: se cura desde raw, sin pedir nada a la fuente.
            cached = since_days is not None and _scraped_within(storage, player_id, since_days)
            try:
                # Se acumula en locales y solo se vuelca al club si el jugador
                # se descarga entero: un 404 a mitad no deja filas parciales.
                profile = client.fetch_player_profile(player_id, slug=member.slug, cached=cached)
                player = {
                    "id": player_id,
                    "slug": member.slug,
                    "name": profile.name or member.name,
                    "birth_date": profile.birth_date,
                    "position": profile.position or member.position,
                    "shirt_number": member.shirt_number,
                    "club_id": club.id,
                    "club_name": club.name,
                }
                values = market_value_rows(
                    client.fetch_market_value(player_id, cached=cached), player_id=player_id
                )
                transfers = transfer_rows(
                    client.fetch_transfers(player_id, cached=cached), player_id=player_id
                )
                availability = availability_rows(
                    client.fetch_performance(player_id, cached=cached), player_id=player_id
                )
                injuries = [
                    _injury_record(injury)
                    for injury in client.fetch_injuries(player_id, slug=member.slug, cached=cached)
                ]
            except SourceHTTPError as error:
                failed += 1
                result.failures.append(PlayerFailure(member.slug, error.url, error.status))
                logger.warning(
                    "transfermarkt %s %s (%d): HTTP %d, saltado",
                    competition,
                    member.slug,
                    player_id,
                    error.status,
                )
                continue

            player_records.append(player)
            value_records += values
            transfer_records += transfers
            availability_records += availability
            injury_records += injuries
            if cached:
                reused += 1
            else:
                downloaded += 1

        logger.info(
            "transfermarkt %s club %s: %d descargados, %d re-curados desde raw, %d fallidos",
            competition,
            club.name,
            downloaded,
            reused,
            failed,
        )

        if not player_records:  # club con todos los jugadores fallidos
            continue

        frames = {
            "transfermarkt_players": _players_frame(player_records),
            "market_values_tm": _values_frame(value_records),
            "transfers": _transfers_frame(transfer_records),
            "availability_tm": _availability_frame(availability_records),
            "injuries_tm": _injuries_frame(injury_records),
        }
        for table, frame in frames.items():
            where = squad_partition if table == "transfermarkt_players" else partition
            storage.curated.upsert_table(table, frame, key=_TABLE_KEYS[table], partition=where)
            result.rows[table] += len(frame)

    return result, seen_player_ids, squad_failures


def _scraped_within(storage: Storage, player_id: int, since_days: int) -> bool:
    """¿Se descargó a este jugador entero en los últimos ``since_days`` días?

    Usa las lesiones como marca de descarga: son la última petición por jugador,
    así que su presencia indica un scrape completo (no uno interrumpido a medias).
    """
    last = storage.raw.last_download_date(
        "transfermarkt", "injuries", f"spieler-{player_id}", extension="html"
    )
    if last is None:
        return False
    return last > datetime.now(tz=UTC).date() - timedelta(days=since_days)


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
