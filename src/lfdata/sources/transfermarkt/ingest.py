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
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from lfdata.sources.http import HttpTransport, SourceHTTPError, scrapeops_proxy_from_env
from lfdata.sources.ingestion import IngestResult, PlayerFailure
from lfdata.sources.transfermarkt.client import (
    PROXY_OVERFLOW,
    SQUAD_VALUE_LEAGUES,
    WAIT_SECONDS,
    TransfermarktClient,
)
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

SQUAD_VALUES_TABLE = "squad_values"
TRANSFERMARKT = "transfermarkt"

# Partición centinela de las tablas de historial para el jugador alcanzado por id,
# no por plantilla (ADR 0013): no pertenece a ningún kader descargado, así que su
# competición de crawl es "bajo demanda". Cuando aparezca luego en un kader de
# la-liga, ``_ingest_clubs`` lo mueve a esa partición (invariante de unicidad).
BAJO_DEMANDA = "bajo-demanda"

# Las cuatro tablas de historial de carrera completa (no ``transfermarkt_players``,
# que es pertenencia a plantilla): las únicas que cura la ingesta por jugador.
HISTORY_TABLES = ("market_values_tm", "transfers", "availability_tm", "injuries_tm")

_CANONICAL_RE = re.compile(r"^p\d+$")
# ``.../profil/spieler/NNN`` (y cualquier URL de Transfermarkt con ``/spieler/NNN``).
_SPIELER_RE = re.compile(r"/spieler/(\d+)")

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


def ingest_player(
    storage: Storage,
    query: str,
    *,
    mappings_dir: str = "mappings",
    transport: HttpTransport | None = None,
    cached: bool = False,
    force: bool = False,
) -> IngestResult:
    """Ingesta dirigida del **historial** de un jugador, sin pertenencia a plantilla.

    Espejo del ``--player`` de SofaScore para el jugador que no está en ninguna
    plantilla descargada (los ``sin-candidato`` enlazados a mano, y el goteo de
    fichajes de Segunda/extranjero antes de aparecer en el kader). Cura **solo las
    cuatro tablas de historial** (:data:`HISTORY_TABLES`) —de carrera completa, no
    por competición (docs/experiments/2026-07-07-alex-fores.md)— y **nunca**
    ``transfermarkt_players``, que es pertenencia a plantilla (ADR 0005). ~5
    peticiones por jugador.

    ``query`` es un ``spieler_id`` numérico, una URL de perfil
    (``.../profil/spieler/NNN``) o un ``canonical_id`` (``p00001``, que se resuelve a
    su ``tm_id`` mapeado). ``cached`` re-cura desde ``raw/`` sin volver a pedir
    (ADR 0003).

    Red de seguridad de identidad (ADR 0013 / pieza del token que aprueba sin
    verificar): si el jugador tiene canónico con mapping de Biwenger, se contrasta
    la fecha de nacimiento del perfil con la de Biwenger y, si discrepa, no se cura
    nada salvo ``--force``.

    El historial va a la partición centinela ``competition=bajo-demanda`` con upsert
    global por jugador (:meth:`CuratedStore.upsert_unique_partition`): si el jugador
    ya vivía en otra partición, se le retira de ella para que no quede duplicado.
    """
    # Import perezoso: ``lfdata.mappings.run`` importa este módulo (ciclo).
    from lfdata.mappings import MappingStore

    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )
    client = TransfermarktClient(transport, storage.raw)
    store = MappingStore(Path(mappings_dir))
    store.load()

    player_id = _resolve_player_id(query, store)
    result = IngestResult(rows=dict.fromkeys(HISTORY_TABLES, 0))
    try:
        # El perfil sirve a la vez de red de seguridad (fecha de nacimiento) y de
        # primera petición del historial; Transfermarkt resuelve por id, así que el
        # slug vacío basta (el cliente rellena un marcador para la URL).
        profile = client.fetch_player_profile(player_id, slug="", cached=cached)
        if not _verify_identity(storage, store, player_id, profile, force=force):
            result.anomalies["jugadores no curados por fecha de nacimiento discrepante"] = 1
            return result
        frames = {
            "market_values_tm": _values_frame(
                market_value_rows(
                    client.fetch_market_value(player_id, cached=cached), player_id=player_id
                )
            ),
            "transfers": _transfers_frame(
                transfer_rows(client.fetch_transfers(player_id, cached=cached), player_id=player_id)
            ),
            "availability_tm": _availability_frame(
                availability_rows(
                    client.fetch_performance(player_id, cached=cached), player_id=player_id
                )
            ),
            "injuries_tm": _injuries_frame(
                [
                    _injury_record(injury)
                    for injury in client.fetch_injuries(player_id, slug="", cached=cached)
                ]
            ),
        }
    except SourceHTTPError as error:
        result.failures.append(PlayerFailure(f"spieler {player_id}", error.url, error.status))
        logger.warning(
            "transfermarkt spieler %d: HTTP %d al pedir su historial, no se cura",
            player_id,
            error.status,
        )
        return result

    partition = {"competition": BAJO_DEMANDA}
    for table, frame in frames.items():
        storage.curated.upsert_unique_partition(
            table, frame, key=_TABLE_KEYS[table], partition=partition
        )
        result.rows[table] = len(frame)
    logger.info(
        "transfermarkt spieler %d (%s): historial curado (bajo-demanda) — "
        "%d valores, %d traspasos, %d disponibilidad, %d lesiones",
        player_id,
        profile.name,
        result.rows["market_values_tm"],
        result.rows["transfers"],
        result.rows["availability_tm"],
        result.rows["injuries_tm"],
    )
    return result


def _resolve_player_id(query: str, store) -> int:
    """Resuelve ``query`` a un ``spieler_id`` de Transfermarkt.

    - ``canonical_id`` (``p\\d+``): busca su ``tm_id`` en los mappings aprobados; si
      no lo tiene, es un error (no hay a quién descargar).
    - URL ``.../profil/spieler/NNN`` (o cualquier URL con ``/spieler/NNN``): NNN.
    - numérico: el ``spieler_id`` directo.
    """
    if _CANONICAL_RE.match(query):
        rows = store.players[
            (store.players["fuente"] == TRANSFERMARKT) & (store.players["canonical_id"] == query)
        ]
        if rows.empty:
            raise ValueError(
                f"{query} no tiene mapping a Transfermarkt todavía; "
                "ingiere por id de Transfermarkt o por URL de perfil."
            )
        return int(rows.iloc[0]["id_en_fuente"])
    match = _SPIELER_RE.search(query)
    if match:
        return int(match.group(1))
    if query.isdigit():
        return int(query)
    raise ValueError(
        f"No sé resolver {query!r} a un spieler_id: pasa un id numérico, una URL "
        ".../profil/spieler/NNN o un canonical_id (pXXXXX)."
    )


def _verify_identity(storage: Storage, store, player_id: int, profile, *, force: bool) -> bool:
    """Red de seguridad: ¿la fecha de nacimiento del perfil casa con la de Biwenger?

    El token de la revisión (``spieler_id`` en ``decision``) aprueba el mapping sin
    verificar, así que la comprobación se hace aquí, al curar. Si el jugador no tiene
    canónico con mapping de Biwenger no hay con qué contrastar y se cura. Solo una
    discrepancia real (ambas fechas presentes y distintas) bloquea; ``force`` la salta.
    """
    from lfdata.mappings.matcher import birthdate_compatible

    canonical_by_tm = store.canonical_by_source(store.players, TRANSFERMARKT)
    canonical = canonical_by_tm.get(str(player_id))
    if not canonical:
        return True
    biw_rows = store.players[
        (store.players["fuente"] == "biwenger") & (store.players["canonical_id"] == canonical)
    ]
    if biw_rows.empty:
        return True
    biw_birth = _biwenger_birth_date(storage, str(biw_rows.iloc[0]["id_en_fuente"]))
    tm_birth = "" if profile.birth_date is None else profile.birth_date.isoformat()
    if birthdate_compatible(biw_birth, tm_birth):
        return True
    if force:
        logger.warning(
            "transfermarkt spieler %d: fecha %s discrepa de la de Biwenger %s "
            "(canónico %s); se cura igual por --force",
            player_id,
            tm_birth,
            biw_birth,
            canonical,
        )
        return True
    logger.warning(
        "transfermarkt spieler %d: fecha %s discrepa de la de Biwenger %s (canónico %s); "
        "NO se cura (revisa el mapping o usa --force)",
        player_id,
        tm_birth,
        biw_birth,
        canonical,
    )
    return False


def _biwenger_birth_date(storage: Storage, biwenger_id: str) -> str:
    """Fecha de nacimiento ISO de un jugador de Biwenger; vacía si no la publica.

    Mira la plantilla actual y el histórico: la ficha de quien ya dejó la liga
    (justo el caso de los alcanzados por id) solo vive en ``_history``.
    """
    for table in ("biwenger_players", "biwenger_players_history"):
        try:
            df = storage.curated.read_table(table)
        except (FileNotFoundError, OSError):
            continue
        if df.empty or "birth_date" not in df.columns or "id" not in df.columns:
            continue
        rows = df[df["id"].astype(str) == str(biwenger_id)]
        for value in rows["birth_date"].dropna():
            text = str(value)[:10]
            if text:
                return text
    return ""


def ingest_squad_values(
    storage: Storage,
    *,
    season: int = DEFAULT_SEASON,
    leagues: Iterable[str] | None = None,
    mappings_dir: str = "mappings",
    transport: HttpTransport | None = None,
    cached: bool = False,
) -> IngestResult:
    """Publica ``squad_values``: valor total de plantilla por club-temporada (issue #69).

    Una petición por liga-temporada (la página de competición), para las 7 ligas de
    :data:`SQUAD_VALUE_LEAGUES` o el subconjunto ``leagues``. Es el nivel de equipo
    (propio/rival) del modelo de rendimiento y, promediando por liga, el nivel de liga
    del baseline de fichajes (docs/implementation/04 y 05).

    Los clubes de La Liga/Segunda se resuelven a ``canonical_team_id`` con los mappings
    de equipo ya aprobados; los extranjeros conservan solo su id de Transfermarkt (no se
    inventan canónicos sin revisión, ADR 0001; no se mapean sus jugadores, ADR 0008).

    Cada liga-temporada es una partición que se reescribe entera (la página trae la
    competición completa de una vez). ``cached`` re-cura desde ``raw/`` sin volver a
    pedir (ADR 0003). ``capture_date`` es la fecha real de descarga del raw, para datar
    la foto del valor al cierre de cada ventana de mercado.
    """
    # Import perezoso: ``lfdata.mappings.run`` importa este módulo, así que traer
    # MappingStore a nivel de módulo cerraría un ciclo de importación.
    from lfdata.mappings import MappingStore

    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )
    client = TransfermarktClient(transport, storage.raw)
    store = MappingStore(Path(mappings_dir))
    store.load()
    canonical_by_tm_team = MappingStore.canonical_by_source(store.teams, TRANSFERMARKT)

    leagues = list(leagues) if leagues is not None else list(SQUAD_VALUE_LEAGUES)
    result = IngestResult(rows={SQUAD_VALUES_TABLE: 0}, stats={"ligas": 0})
    for league in leagues:
        clubs = client.fetch_competition_clubs(league, season=season, cached=cached)
        # Sin ``cached`` acabamos de descargar hoy: la fecha de captura es hoy, sin
        # reconsultar el store. Solo re-curando (``cached``) hay que averiguar cuándo
        # se bajó lo que ya está en raw/.
        if cached:
            code = SQUAD_VALUE_LEAGUES[league][1]
            capture_date = storage.raw.last_download_date(
                TRANSFERMARKT, "competition-clubs", f"{code}-saison-{season}", extension="html"
            )
        else:
            capture_date = datetime.now(tz=UTC).date()
        records = [
            {
                "club_id": club.id,
                "club_name": club.name,
                "squad_value": club.squad_value,
                "canonical_team_id": canonical_by_tm_team.get(str(club.id), ""),
                "capture_date": capture_date,
            }
            for club in clubs
        ]
        frame = _squad_values_frame(records)
        storage.curated.write_table(
            SQUAD_VALUES_TABLE,
            frame,
            partition={"competition": league, "season": str(season)},
        )
        result.rows[SQUAD_VALUES_TABLE] += len(frame)
        result.stats["ligas"] += 1
        logger.info(
            "transfermarkt squad_values %s %d: %d clubes (valor plantilla)",
            league,
            season,
            len(frame),
        )
    return result


def _squad_values_frame(records: list[dict]) -> pd.DataFrame:
    columns = ["club_id", "club_name", "squad_value", "canonical_team_id", "capture_date"]
    df = pd.DataFrame(records, columns=columns)
    df["capture_date"] = pd.to_datetime(df["capture_date"], errors="coerce")
    return df.astype({"club_id": "Int64", "squad_value": "Int64"})


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
            if table == "transfermarkt_players":
                # Pertenencia a plantilla: particionada por (competition, season), un
                # jugador puede estar en varias temporadas. Upsert normal por partición.
                storage.curated.upsert_table(
                    table, frame, key=_TABLE_KEYS[table], partition=squad_partition
                )
            else:
                # Historial de carrera completa: cada jugador vive en exactamente una
                # partición. Al escribirlo aquí se retira de las demás (bajo-demanda,
                # otra competición), evitando el duplicado latente (ADR 0013).
                storage.curated.upsert_unique_partition(
                    table, frame, key=_TABLE_KEYS[table], partition=partition
                )
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
