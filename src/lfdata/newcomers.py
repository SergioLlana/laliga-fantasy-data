"""Detector de jugador nuevo y descarga bajo demanda (paso 5, orden de trabajo 1).

Un **fichaje** es un jugador que aparece en la plantilla de una competición de
Biwenger sin puntos en ninguna temporada anterior **de esa misma competición**: no
tiene historial de La Liga del que aprender, así que su proyección tendrá que
salir de su historial en otra liga (el baseline de fichajes). Este módulo lo
detecta en la ingesta diaria y dispara, sin intervención humana, las dos cosas que
ese baseline necesita:

1. **Identidad** — refresca la plantilla de Transfermarkt de su club de llegada
   (una sola plantilla, no la competición entera) y ejecuta el matcher. El fichaje
   sale con ID canónico o, si el par es dudoso, encolado a revisión.
2. **Historial** — descarga de SofaScore su historial completo, venga de la liga
   que venga (el mecanismo bajo demanda de #11).

El ascendido de Segunda **también** es un fichaje (decidido el 2026-07-13): Segunda
es una liga de origen como el Championship o el Brasileirão, y su salto se estima
con el mismo método —eventing de SofaScore y valor de Transfermarkt, corregidos
por el nivel de la liga de origen— y no con sus puntos de Biwenger en Segunda. Que
esos puntos existan no cambia el baseline; lo que permiten es **validarlo**, que es
justo lo que hace el experimento Forés: el único salto donde conocemos la verdad en
ambos lados.

El registro queda en la tabla curada ``newcomers``, con grano jugador-temporada de
debut, que hace además de marca de idempotencia: un fichaje cuyo historial ya se
descargó no se vuelve a pedir. Ningún fallo de una fuente aborta el run —el
fichaje queda registrado con lo que se pudo conseguir y el run siguiente lo
reintenta—, porque esto vive dentro del pipeline diario y un jugador no puede
tirarlo.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from lfdata.mappings import run_map
from lfdata.mappings.store import (
    BIWENGER,
    SOFASCORE_PLAYER_REVIEW_COLUMNS,
    TRANSFERMARKT,
    MappingStore,
)
from lfdata.sources.http import (
    HttpTransport,
    SourceHTTPError,
    scrapeops_proxy_from_env,
)
from lfdata.sources.ingestion import IngestResult, PlayerFailure
from lfdata.sources.sofascore import ingest_player, resolve_identity_by_search
from lfdata.sources.sofascore.client import (
    PROXY_OVERFLOW,
    WAIT_SECONDS,
    SofaScoreClient,
)
from lfdata.sources.transfermarkt import ingest_clubs
from lfdata.sources.transfermarkt import ingest_player as ingest_tm_player
from lfdata.storage import Storage

logger = logging.getLogger(__name__)

TABLE = "newcomers"

# Las dos tablas de puntos de Biwenger: el detalle por jugador (solo los que
# siguen en la competición) y la de jornadas (todos los que puntuaron, incluidos
# los que se fueron). Un jugador con historial en cualquiera de las dos no es un
# fichaje.
POINTS_TABLES = ("fantasy_points", "fantasy_round_points")

# Estado de la descarga del historial, y marca de si hay que reintentarla. Solo
# ``descargado`` es terminal (identidad verificada + historial curado); el resto
# se reintenta en el run siguiente.
DOWNLOADED = "descargado"
NO_HISTORY = "sin-historial"
FAILED = "fallo"
# La identidad de SofaScore no se pudo verificar (sin canónico de Biwenger todavía,
# o la búsqueda es ambigua/discrepante): no se cura nada y se reintenta. Es lo que
# evita fosilizar el historial de la persona equivocada (issue #81).
NO_IDENTITY = "sin-identidad"

# El registro dice quién llegó, a qué equipo y si su historial está descargado.
# No guarda el id de SofaScore: los ids de cada fuente viven en los mappings
# (ADR 0001), y duplicarlos aquí sería una segunda verdad que se puede desalinear.
NEWCOMER_COLUMNS = [
    "player_id",
    "name",
    "team_id",
    "canonical_id",
    "history",
    "detected_on",
]

# Al refrescar la plantilla de Transfermarkt del club de llegada, sus jugadores
# ya bajados hace menos de estos días se vuelven a curar desde raw/ en vez de
# re-pedirse: el fichaje es el único al que hay que ir de verdad a la fuente.
SINCE_DAYS = 7

# Métricas del run para el resumen del CLI.
DETECTED = "fichajes detectados"
ALREADY = "fichajes ya registrados"
WITH_HISTORY = "fichajes con historial descargado"
IN_REVIEW = "fichajes encolados a revisión de mapping"
DEFERRED = "fichajes aplazados al siguiente run (tope)"
# Fichajes cuya descarga se aplaza porque su identidad de SofaScore no está
# verificada: sin ella no se cura nada (issue #81, criterio de aceptación).
DEFERRED_IDENTITY = "fichajes aplazados por identidad de SofaScore pendiente"

# Anomalía: la fuente no tiene al jugador, así que no hay historial que curar.
NO_PROFILE = "fichajes sin ficha en SofaScore"

# Fichajes cuya identidad de Transfermarkt está aprobada (a mano, vía token de
# Fase 1) pero cuyo tm_id sigue colgante —sin historial en ``market_values_tm``—
# porque no aparecen en el kader de su club de llegada (filial, lag de TM): se les
# trae la carrera por id, cerrando la ventana del patrón (issue #92).
TM_HISTORY = "fichajes con historial de Transfermarkt traído por id"

SOFASCORE = "sofascore"


@dataclass(frozen=True)
class Newcomer:
    """Un jugador de la plantilla actual sin puntos en temporadas anteriores."""

    player_id: int
    name: str
    team_id: int | None
    # Fecha de nacimiento de Biwenger (``YYYY-MM-DD`` o vacía): la evidencia con
    # la que se verifica la identidad de SofaScore antes de bajar el historial.
    birth_date: str = ""


def detect_newcomers(storage: Storage, competition: str, season: int) -> list[Newcomer]:
    """Jugadores de la plantilla de ``competition`` sin historial de puntos en ella.

    El historial se mira en las temporadas **anteriores** a ``season`` y solo en
    ``competition``: quien sube de Segunda no tiene puntos de La Liga y por tanto es
    un fichaje, igual que el que llega del Brasileirão —Segunda es una liga de
    origen más, no un atajo—. Los puntos que un fichaje lleve ya en la temporada en
    curso no le quitan la condición: lo que le falta es historial del que proyectar,
    no minutos.
    """
    squad = storage.curated.read_partition(
        "biwenger_players", partition={"competition": competition}
    )
    if squad.empty:
        return []

    has_birth = "birth_date" in squad.columns
    veterans = _players_with_points_before(storage, competition, season)
    newcomers = [
        Newcomer(
            player_id=int(row.id),
            name=str(row.name),
            team_id=None if pd.isna(row.team_id) else int(row.team_id),
            birth_date=_birth_date(row) if has_birth else "",
        )
        for row in squad.itertuples()
        if not pd.isna(row.id) and int(row.id) not in veterans
    ]
    return sorted(newcomers, key=lambda n: n.player_id)


def _birth_date(row) -> str:
    """Fecha de nacimiento ISO de una fila de plantilla; vacía si no la tiene."""
    value = getattr(row, "birth_date", None)
    return "" if value is None or pd.isna(value) else str(value)[:10]


def ingest_newcomers(
    storage: Storage,
    competition: str,
    season: int,
    *,
    mappings_dir: str = "mappings",
    since_days: int | None = SINCE_DAYS,
    max_newcomers: int | None = None,
    dry_run: bool = False,
    sofascore_transport: HttpTransport | None = None,
    transfermarkt_transport: HttpTransport | None = None,
) -> IngestResult:
    """Detecta los fichajes de la temporada y les trae identidad e historial.

    Idempotente: un fichaje ya registrado con su historial descargado no vuelve a
    generar ni una petición. Los que quedaron en ``sin-historial`` o ``fallo`` se
    reintentan en el run siguiente. Con ``dry_run`` solo detecta y registra en el
    log, sin descargar nada ni escribir tablas.

    ``max_newcomers`` acota cuántos se resuelven en este run; el resto se aplaza al
    siguiente. Es la válvula de seguridad del pipeline: un fichaje son decenas de
    peticiones a dos fuentes, y la detección depende de que el histórico de puntos
    esté completo —con un backfill a medias, o si una ingesta de Biwenger falla y
    deja ``fantasy_points`` corto, media plantilla parece recién llegada—. Con el
    tope, un run anómalo cuesta N descargas y se ve en el resumen, en vez de
    lanzarle cientos de peticiones a SofaScore.
    """
    detected = detect_newcomers(storage, competition, season)
    done = _already_downloaded(storage, competition)
    pending = [n for n in detected if n.player_id not in done]

    result = IngestResult(
        rows={TABLE: 0},
        stats={DETECTED: len(detected), ALREADY: len(detected) - len(pending)},
    )
    if max_newcomers is not None and len(pending) > max_newcomers:
        logger.warning(
            "newcomers %s %d: %d fichajes pendientes, más que el tope de %d; se resuelven "
            "los %d primeros y el resto queda para el run siguiente",
            competition,
            season,
            len(pending),
            max_newcomers,
            max_newcomers,
        )
        result.stats[DEFERRED] = len(pending) - max_newcomers
        pending = pending[:max_newcomers]

    for newcomer in pending:
        logger.info(
            "jugador nuevo en %s %d: %s (biwenger %d, equipo %s)",
            competition,
            season,
            newcomer.name,
            newcomer.player_id,
            newcomer.team_id,
        )
    if not pending or dry_run:
        logger.info(
            "newcomers %s %d: %d detectados, %d ya registrados%s",
            competition,
            season,
            len(detected),
            result.stats[ALREADY],
            ", nada que descargar (dry-run)" if dry_run and pending else "",
        )
        return result

    store = _resolve_identity(
        storage,
        competition,
        season,
        pending,
        mappings_dir=mappings_dir,
        since_days=since_days,
        transport=transfermarkt_transport,
        result=result,
    )
    canonical_by_biwenger = MappingStore.canonical_by_source(store.players, BIWENGER)
    in_review = {str(v) for v in store.players_review["biwenger_id"]}
    result.stats[IN_REVIEW] = len(in_review & {str(n.player_id) for n in pending})
    result.stats[DEFERRED_IDENTITY] = 0

    # Un único transporte con su limitador de ritmo para todas las peticiones de
    # SofaScore del run (búsqueda, ficha e historial), en vez de uno por fichaje.
    sofascore_transport = sofascore_transport or _default_sofascore_transport()

    today = datetime.now(tz=UTC).date().isoformat()
    rows = []
    for newcomer in pending:
        canonical_id = canonical_by_biwenger.get(str(newcomer.player_id), "")
        history = _download_history(
            storage,
            newcomer,
            canonical_id,
            store=store,
            mappings_dir=mappings_dir,
            transport=sofascore_transport,
            result=result,
            today=today,
        )
        rows.append(
            {
                "player_id": newcomer.player_id,
                "name": newcomer.name,
                "team_id": newcomer.team_id,
                "canonical_id": canonical_id,
                "history": history,
                "detected_on": today,
            }
        )

    storage.curated.upsert_table(
        TABLE,
        _newcomers_frame(rows),
        key="player_id",
        partition={"competition": competition, "season": str(season)},
    )
    result.rows[TABLE] = len(rows)
    result.stats[WITH_HISTORY] = sum(1 for row in rows if row["history"] == DOWNLOADED)
    logger.info(
        "newcomers %s %d: %d detectados, %d ya registrados, %d nuevos registrados "
        "(%d con historial, %d en revisión de mapping, %d aplazados por identidad)",
        competition,
        season,
        len(detected),
        result.stats[ALREADY],
        len(rows),
        result.stats[WITH_HISTORY],
        result.stats[IN_REVIEW],
        result.stats[DEFERRED_IDENTITY],
    )
    return result


def _default_sofascore_transport() -> HttpTransport:
    """Transporte de SofaScore por defecto (impersonación + desbordo a ScrapeOps)."""
    return HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )


def _resolve_identity(
    storage: Storage,
    competition: str,
    season: int,
    pending: list[Newcomer],
    *,
    mappings_dir: str,
    since_days: int | None,
    transport: HttpTransport | None,
    result: IngestResult,
) -> MappingStore:
    """Trae a Transfermarkt los clubes de llegada, mapea y devuelve los mappings.

    Sin la plantilla de Transfermarkt del club, el fichaje no tiene contraparte a
    la que mapear y su historial de SofaScore se quedaría huérfano (sin
    ``canonical_id``, ninguna tabla curada canónica lo admite). Refrescamos solo
    esos clubes —uno o dos en un día normal— y ejecutamos el matcher: cada fichaje
    sale aprobado o encolado a revisión, y el run sigue en ambos casos.
    """
    club_ids = _transfermarkt_clubs(mappings_dir, {n.team_id for n in pending})
    if club_ids:
        _add(
            result,
            ingest_clubs(
                storage,
                competition,
                club_ids,
                season=season,
                transport=transport,
                since_days=since_days,
            ),
        )

    report = run_map(storage, mappings_dir, season=season)
    logger.info(
        "newcomers %s %d: matcher tras el refresh — %d/%d jugadores mapeados, %d en revisión",
        competition,
        season,
        report.players_mapped,
        report.players_total,
        report.players_review,
    )

    store = MappingStore(Path(mappings_dir))
    store.load()

    _download_dangling_tm_history(
        storage,
        pending,
        store,
        mappings_dir=mappings_dir,
        transport=transport,
        result=result,
    )
    return store


def _download_dangling_tm_history(
    storage: Storage,
    pending: list[Newcomer],
    store: MappingStore,
    *,
    mappings_dir: str,
    transport: HttpTransport | None,
    result: IngestResult,
) -> None:
    """Trae por id el historial de los fichajes con TM aprobado pero fuera del kader.

    Simetría con la descarga de SofaScore por id ya verificado
    (:func:`_download_by_id`): el token de Fase 1 aprueba el mapping de Transfermarkt
    sin datos, y aquí se cierra la ventana descargando su carrera cuando el fichaje
    llega de una filial o con lag de Transfermarkt y no está en el kader de su club.

    El fichaje que **sí** estaba en el kader ya tiene historial —lo curó
    ``ingest_clubs`` en el refresh de identidad—, así que la comprobación de colgante
    (sin filas en ``market_values_tm``) lo excluye por sí sola. Ningún fallo aborta el
    run: ``ingest_player`` registra el fallo y se reintenta el run siguiente.
    """
    canonical_by_biwenger = MappingStore.canonical_by_source(store.players, BIWENGER)
    canonical_to_tm = {
        canonical: tm_id
        for tm_id, canonical in MappingStore.canonical_by_source(
            store.players, TRANSFERMARKT
        ).items()
    }
    history_ids = _tm_history_ids(storage)

    for newcomer in pending:
        canonical = canonical_by_biwenger.get(str(newcomer.player_id))
        tm_id = canonical_to_tm.get(canonical) if canonical else None
        if tm_id is None or tm_id in history_ids:
            continue
        downloaded = ingest_tm_player(
            storage, tm_id, mappings_dir=mappings_dir, transport=transport
        )
        _add(result, downloaded)
        if downloaded.failures:
            continue
        result.stats[TM_HISTORY] = result.stats.get(TM_HISTORY, 0) + 1
        logger.info(
            "fichaje %s (biwenger %d): historial de Transfermarkt (spieler %s) traído por id "
            "(fuera del kader), %d valores de mercado",
            newcomer.name,
            newcomer.player_id,
            tm_id,
            downloaded.rows.get("market_values_tm", 0),
        )


def _tm_history_ids(storage: Storage) -> set[str]:
    """IDs de Transfermarkt con historial ya curado (una fila en ``market_values_tm``)."""
    try:
        df = storage.curated.read_table("market_values_tm")
    except (FileNotFoundError, OSError):
        return set()
    if df.empty or "player_id" not in df.columns:
        return set()
    return {str(int(v)) for v in df["player_id"].dropna()}


def _transfermarkt_clubs(mappings_dir: str, team_ids: Iterable[int | None]) -> set[int]:
    """IDs de club de Transfermarkt de los equipos de Biwenger dados.

    Un equipo aún sin mapping (un recién ascendido en su primera ronda, p. ej.) no
    aporta club: se avisa y se sigue. El fichaje se queda sin contraparte de
    Transfermarkt hasta que se apruebe el mapping de su equipo, pero su historial
    de SofaScore se descarga igual y el run no se cae.
    """
    store = MappingStore(Path(mappings_dir))
    store.load()
    canonical_by_biwenger = MappingStore.canonical_by_source(store.teams, BIWENGER)
    tm_by_canonical = {
        canonical: tm_id
        for tm_id, canonical in MappingStore.canonical_by_source(store.teams, TRANSFERMARKT).items()
    }

    clubs: set[int] = set()
    for team_id in team_ids:
        canonical = canonical_by_biwenger.get(str(team_id)) if team_id is not None else None
        tm_id = tm_by_canonical.get(canonical) if canonical else None
        if tm_id is None:
            logger.warning(
                "equipo biwenger %s sin club de Transfermarkt mapeado: no se refresca su plantilla",
                team_id,
            )
            continue
        clubs.add(int(tm_id))
    return clubs


def _download_history(
    storage: Storage,
    newcomer: Newcomer,
    canonical_id: str,
    *,
    store: MappingStore,
    mappings_dir: str,
    transport: HttpTransport,
    result: IngestResult,
    today: str,
) -> str:
    """Verifica la identidad de SofaScore y **solo entonces** descarga el historial.

    Primero identidad, después descarga: nunca «al primero que salga» (issue #81).

    1. **Ya mapeado o en el catálogo** — ``lfdata map`` (ejecutado en la fase de
       identidad) ya colgó su id de SofaScore del canónico: se descarga por ID.
    2. **Sin canónico** — su Transfermarkt sigue en revisión, así que no hay de qué
       colgar la identidad de SofaScore: se aplaza (``sin-identidad``) sin gastar
       peticiones y el run siguiente lo reintenta cuando la revisión se resuelva.
    3. **Fuera del catálogo, con canónico** — se resuelve por ``search/all`` con una
       única petición de verificación (:func:`_resolve_via_search`).

    Ninguna fila entra en el eventing curado sin ``canonical_id`` verificado: el
    mapping se aprueba **antes** de descargar, para que ``ingest_player`` lo estampe.
    """
    sofascore_id = _sofascore_id(store, canonical_id)
    if sofascore_id:
        return _download_by_id(storage, newcomer, sofascore_id, mappings_dir, transport, result)
    if not canonical_id:
        return _defer_no_identity(
            newcomer, result, "sin canónico de Biwenger (Transfermarkt en revisión)"
        )
    return _resolve_via_search(
        storage, newcomer, canonical_id, store, mappings_dir, transport, result, today
    )


def _resolve_via_search(
    storage: Storage,
    newcomer: Newcomer,
    canonical_id: str,
    store: MappingStore,
    mappings_dir: str,
    transport: HttpTransport,
    result: IngestResult,
    today: str,
) -> str:
    """Resuelve por búsqueda la identidad de un fichaje fuera del catálogo.

    Candidato único cuya fecha de nacimiento coincide con la de Biwenger: se
    aprueba el mapping y se descarga por ID. Cero o varios candidatos, o fecha
    discrepante: se encola a revisión con la evidencia y se aplaza, sin curar nada
    ni gastar peticiones en verificar a ciegas.
    """
    client = SofaScoreClient(transport, storage.raw)
    try:
        identity = resolve_identity_by_search(client, newcomer.name, newcomer.birth_date)
    except SourceHTTPError as error:
        logger.warning(
            "fichaje %s (biwenger %d): HTTP %d al buscar su identidad, se reintentará",
            newcomer.name,
            newcomer.player_id,
            error.status,
        )
        result.failures.append(PlayerFailure(newcomer.name, error.url, error.status))
        return FAILED

    if identity.verified_id is not None:
        verified = str(identity.verified_id)
        if verified in MappingStore.approved_ids(store.players, SOFASCORE):
            # El id ya cuelga de otro canónico: conflicto real, no se toca. A revisión.
            _enqueue_review(store, newcomer, identity.candidates, "candidato-compartido")
            return _defer_no_identity(
                newcomer, result, "id de SofaScore ya mapeado a otro canónico"
            )
        # Se aprueba el mapping antes de descargar, para que ``ingest_player`` estampe
        # el canónico en cada fila del eventing (ninguna fila sin canónico verificado).
        store.add_player(canonical_id, [(SOFASCORE, verified)], method="auto", date=today)
        store.save()
        logger.info(
            "fichaje %s (biwenger %d): identidad de SofaScore %s verificada por fecha "
            "(%s), mapping aprobado",
            newcomer.name,
            newcomer.player_id,
            verified,
            newcomer.birth_date or "sin fecha",
        )
        return _download_by_id(storage, newcomer, verified, mappings_dir, transport, result)

    if not identity.candidates:
        logger.warning(
            "fichaje %s (biwenger %d): SofaScore no tiene ficha de fútbol para %r, sin historial",
            newcomer.name,
            newcomer.player_id,
            newcomer.name,
        )
        result.anomalies[NO_PROFILE] = result.anomalies.get(NO_PROFILE, 0) + 1
        return NO_HISTORY

    _enqueue_review(store, newcomer, identity.candidates, identity.motivo)
    return _defer_no_identity(
        newcomer, result, f"búsqueda ambigua ({identity.motivo}), encolado a revisión"
    )


def _download_by_id(
    storage: Storage,
    newcomer: Newcomer,
    sofascore_id: str,
    mappings_dir: str,
    transport: HttpTransport,
    result: IngestResult,
) -> str:
    """Descarga el historial por un id de SofaScore ya verificado y lo cura."""
    try:
        downloaded = ingest_player(
            storage, sofascore_id, mappings_dir=mappings_dir, transport=transport
        )
    except SourceHTTPError as error:
        logger.warning(
            "fichaje %s (biwenger %d): HTTP %d al pedir su historial, se reintentará",
            newcomer.name,
            newcomer.player_id,
            error.status,
        )
        result.failures.append(PlayerFailure(newcomer.name, error.url, error.status))
        return FAILED

    _add(result, downloaded)
    logger.info(
        "fichaje %s (biwenger %d): historial de SofaScore descargado, %d partidos",
        newcomer.name,
        newcomer.player_id,
        downloaded.rows.get("player_match_stats", 0),
    )
    return DOWNLOADED


def _defer_no_identity(newcomer: Newcomer, result: IngestResult, reason: str) -> str:
    """Aplaza un fichaje cuya identidad de SofaScore no se pudo verificar."""
    logger.info(
        "fichaje %s (biwenger %d): identidad de SofaScore sin verificar (%s), se aplaza",
        newcomer.name,
        newcomer.player_id,
        reason,
    )
    result.stats[DEFERRED_IDENTITY] = result.stats.get(DEFERRED_IDENTITY, 0) + 1
    return NO_IDENTITY


def _enqueue_review(
    store: MappingStore, newcomer: Newcomer, candidates: list[dict], motivo: str
) -> None:
    """Encola el fichaje a ``sofascore-review.csv`` con la evidencia de ambos lados.

    Reutiliza el mismo fichero y formato que el matcher de #74: al rellenar la
    ``decision`` a mano y re-ejecutar, ``lfdata map`` (que corre en la fase de
    identidad del run siguiente) aplica el mapping y el historial baja por ID. Un
    candidato con ``sofascore_id`` vacío deja al revisor completar el id él mismo.
    """
    biw_id = str(newcomer.player_id)
    rows = [
        {
            "biwenger_id": biw_id,
            "biwenger_name": newcomer.name,
            "biwenger_team": "" if newcomer.team_id is None else str(newcomer.team_id),
            "biwenger_birth_date": newcomer.birth_date or "",
            "sofascore_id": candidate["id"],
            "sofascore_name": candidate["name"],
            "sofascore_team": candidate["team"],
            "sofascore_birth_date": candidate["birth_date"],
            "motivo": motivo,
            "decision": "",
        }
        for candidate in candidates
    ]
    # Se reemplazan las filas previas de este fichaje para no acumular duplicados
    # entre runs (el matcher no las regenera: el fichaje no está en el catálogo).
    kept = store.players_review_sofascore[
        store.players_review_sofascore["biwenger_id"].astype(str) != biw_id
    ]
    store.players_review_sofascore = pd.concat(
        [kept, pd.DataFrame(rows, columns=SOFASCORE_PLAYER_REVIEW_COLUMNS)],
        ignore_index=True,
    )
    store.save()


def _sofascore_id(store: MappingStore, canonical_id: str) -> str | None:
    """ID de SofaScore ya mapeado de esta identidad canónica, si lo tiene."""
    if not canonical_id:
        return None
    rows = store.players[
        (store.players["fuente"] == SOFASCORE) & (store.players["canonical_id"] == canonical_id)
    ]
    return None if rows.empty else str(rows.iloc[0]["id_en_fuente"])


def _players_with_points_before(storage: Storage, competition: str, season: int) -> set[int]:
    """IDs de Biwenger con puntos en ``competition`` antes de ``season``."""
    ids: set[int] = set()
    for table in POINTS_TABLES:
        try:
            df = storage.curated.read_table(table)
        except (FileNotFoundError, OSError):
            continue
        if df.empty or not {"player_id", "season", "competition"} <= set(df.columns):
            continue
        past = df[
            (df["competition"] == competition)
            & (pd.to_numeric(df["season"], errors="coerce") < season)
        ]
        ids |= {int(v) for v in past["player_id"].dropna()}
    return ids


def _already_downloaded(storage: Storage, competition: str) -> set[int]:
    """IDs de Biwenger ya registrados como fichaje con su historial descargado.

    Se mira en todas las temporadas de la tabla, no solo en la que se ingiere: un
    fichaje que debutó hace dos temporadas y nunca llegó a puntuar sigue sin
    historial de Biwenger, así que el detector vuelve a verlo cada temporada. Su
    registro previo es lo que evita re-descargarle a SofaScore año tras año.
    """
    try:
        df = storage.curated.read_table(TABLE)
    except (FileNotFoundError, OSError):
        return set()
    if df.empty:
        return set()
    done = df[(df["competition"] == competition) & (df["history"] == DOWNLOADED)]
    return {int(v) for v in done["player_id"].dropna()}


def _newcomers_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=NEWCOMER_COLUMNS).astype({"team_id": "Int64"})


def _add(result: IngestResult, other: IngestResult) -> None:
    """Acumula ``other`` sobre ``result``, sumando las filas de cada tabla.

    ``IngestResult.merge`` sobrescribe las tablas que coinciden (nació para unir
    resultados de tablas disjuntas); aquí se llama varias veces a la misma ingesta
    —un ``ingest_player`` por fichaje— y lo que hace falta es sumar.
    """
    for table, count in other.rows.items():
        result.rows[table] = result.rows.get(table, 0) + count
    for reason, count in other.anomalies.items():
        result.anomalies[reason] = result.anomalies.get(reason, 0) + count
    result.failures.extend(other.failures)
