"""Ingesta de la plantilla de una competición a las tablas curadas."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from lfdata.sources.biwenger.client import PROXY_OVERFLOW, WAIT_SECONDS, BiwengerClient
from lfdata.sources.biwenger.models import CompetitionData, Player, PlayerDetail, RoundData
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

# Biwenger publica a los entrenadores en la misma lista que a los jugadores, con
# ficha propia (precio, puntos): Flick, Mourinho, Simeone... No son jugadores —no
# existen en la plantilla de Transfermarkt ni tienen minutos ni eventing—, así que
# quedan fuera de biwenger_players y de sus puntos. Sin este filtro contaminan el
# mapping: el entrenador Simeone compite con su hijo Giuliano por la misma ficha de
# Transfermarkt y ninguno de los dos llega a aprobarse.
COACH_POSITION = 5

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


def squad_players(data: CompetitionData) -> list[Player]:
    """Los jugadores de la plantilla, sin los entrenadores (:data:`COACH_POSITION`)."""
    return [player for player in data.players.values() if player.position != COACH_POSITION]


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

    La plantilla no trae fecha de nacimiento, así que las ``birth_date`` ya
    presentes en el destino (aportadas por la ingesta de reports, #37) se
    conservan; sin esto, cada refresco de plantilla las pondría a nulo.

    Devuelve el número de filas escritas por tabla.
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )
    client = BiwengerClient(transport, storage.raw)
    data = client.fetch_competition_data(competition).data

    partition = {"competition": competition}
    existing = storage.curated.read_partition("biwenger_players", partition=partition)
    births: dict[int, str | None] = {}
    if "birth_date" in existing.columns:
        known = existing.dropna(subset=["birth_date"])
        births = dict(zip(known["id"].astype(int), known["birth_date"], strict=True))

    players = _players_frame(squad_players(data), births)
    teams = pd.DataFrame([team.model_dump() for team in data.teams.values()])

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


def _refresh_players(
    client: BiwengerClient,
    storage: Storage,
    competition: str,
    season: str,
    players: list[Player],
    *,
    batch_size: int,
) -> IngestResult:
    """Descarga el detalle de ``players`` y vuelca fantasy_points/prices/birthday.

    El núcleo compartido por el recorrido completo (:func:`ingest_reports`) y el
    refresh por deltas (:func:`ingest_reports_delta`): la única diferencia entre
    ambos es qué jugadores llegan aquí. Vuelca por lotes con ``upsert_table``
    (clave ``player_id``) durante el recorrido, de modo que un fallo a mitad
    conserva en curated todo lo ya descargado y reejecutar no duplica. Un jugador
    que la fuente ya no sirve (404) se registra como fallo y se salta sin abortar.

    El detalle trae la fecha de nacimiento (``birthday``), que la plantilla no
    publica: se refresca en ``biwenger_players`` (upsert por ``id``, misma
    partición de competición) para endurecer el matching de identidad (#37).
    """
    partition = {"competition": competition, "season": season}
    result = IngestResult(rows={"fantasy_points": 0, "biwenger_prices": 0})
    points_without_stats = 0
    total = len(players)
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

    for index, player in enumerate(players, start=1):
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
    return result


# Métricas del recorrido de reports para el resumen del run.
REFRESHED = "jugadores refrescados"
SKIPPED = "jugadores saltados"


def _players_to_refresh(
    storage: Storage,
    competition: str,
    season: str,
    players: list[Player],
    *,
    since_days: int | None,
    resume: bool,
) -> tuple[list[Player], int]:
    """Divide la plantilla en (a descargar, nº saltados) por reanudación.

    Un jugador cuyo report de esta temporada ya está en ``raw/`` se salta para no
    re-descargarlo al reanudar un backfill cortado (o por la cuota):

    - con ``resume`` (temporada pasada, inmutable) basta con que el raw exista, sin
      ventana de días;
    - con ``since_days`` (temporada actual) solo si se bajó hace menos de N días.

    El raw se escribe de forma atómica y solo tras un 2xx (un 404 o corte lanza
    antes de guardar), así que su presencia marca una descarga completa, nunca una
    interrumpida a medias. Sin ninguno de los dos modos, no se salta a nadie.
    """
    if not resume and since_days is None:
        return players, 0
    cutoff = datetime.now(tz=UTC).date() - timedelta(days=since_days) if since_days else None
    to_refresh: list[Player] = []
    skipped = 0
    for player in players:
        last = storage.raw.last_download_date(
            "biwenger", "player-reports", f"{competition}-{player.slug}-{season}"
        )
        already = last is not None and (resume or (cutoff is not None and last > cutoff))
        if already:
            skipped += 1
        else:
            to_refresh.append(player)
    return to_refresh, skipped


def ingest_reports(
    storage: Storage,
    competition: str,
    season: str,
    *,
    transport: HttpTransport | None = None,
    batch_size: int = REPORTS_BATCH_SIZE,
    since_days: int | None = None,
    resume: bool = False,
) -> IngestResult:
    """Publica fantasy_points y biwenger_prices recorriendo la plantilla entera.

    Descarga el detalle de los ~634 jugadores de la plantilla. Es el recorrido
    completo de una temporada (bootstrap y backfill); para el mantenimiento
    diario tras jornada, :func:`ingest_reports_delta` refresca solo a quienes
    puntuaron. Devuelve las filas escritas por tabla y los jugadores fallidos.

    Para reanudar un backfill cortado sin repetir peticiones, ``resume`` (temporada
    pasada inmutable) salta a quien ya tiene su report en ``raw/`` y ``since_days``
    (temporada actual) a quien se bajó hace menos de N días; los saltados cuentan
    como vistos en ``stats``. Sin ninguno de los dos, recorre la plantilla entera.
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )
    client = BiwengerClient(transport, storage.raw)
    data = client.fetch_competition_data(competition).data
    to_refresh, skipped = _players_to_refresh(
        storage,
        competition,
        season,
        squad_players(data),
        since_days=since_days,
        resume=resume,
    )
    result = _refresh_players(
        client, storage, competition, season, to_refresh, batch_size=batch_size
    )
    result.stats = {REFRESHED: len(to_refresh), SKIPPED: skipped}
    logger.info(
        "biwenger reports %s %s: %d refrescados, %d saltados, %d filas de puntos, %d de precios, "
        "%d fallidos, %d reports con puntos sin rawStats",
        competition,
        season,
        len(to_refresh),
        skipped,
        result.rows["fantasy_points"],
        result.rows["biwenger_prices"],
        len(result.failures),
        result.anomalies.get(POINTS_WITHOUT_STATS, 0),
    )
    return result


def _scoring_player_ids(round_data: RoundData) -> set[int]:
    """Ids de todos los jugadores que puntuaron en la jornada (ambos equipos)."""
    return {
        report.player.id
        for game in round_data.games
        for team in (game.home, game.away)
        for report in team.reports
    }


def ingest_reports_delta(
    storage: Storage,
    competition: str,
    season: str,
    *,
    transport: HttpTransport | None = None,
    batch_size: int = REPORTS_BATCH_SIZE,
) -> IngestResult:
    """Refresh por deltas tras jornada: solo refresca a quienes puntuaron.

    En lugar de recorrer los ~634 de la plantilla, mira el catálogo de jornadas
    que la plantilla ya trae (``season.rounds``), detecta las jornadas terminadas
    que aún no están en ``fantasy_points`` y, por cada una, pide la jornada vía
    rounds (1 petición) para obtener la lista exacta de quienes puntuaron (~280).
    Solo esos jugadores de la plantilla refrescan su detalle (reports, precios,
    birthday). Como una jornada solo genera fila en ``fantasy_points`` para quien
    puntuó, el resultado es idéntico al del recorrido completo, con una fracción
    de las peticiones.

    Sin jornada nueva desde el último run, no pide ningún detalle. Idempotente:
    ``fantasy_points`` es la marca de qué jornadas ya se procesaron, así que
    reejecutar no vuelve a pedir nada. Devuelve las filas escritas y, en
    ``stats``, jugadores refrescados vs. saltados.
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )
    client = BiwengerClient(transport, storage.raw)
    data = client.fetch_competition_data(competition).data
    squad = {player.id: player for player in squad_players(data)}

    partition = {"competition": competition, "season": season}
    processed = {
        int(round_id)
        for round_id in storage.curated.distinct_values(
            "fantasy_points", "round_id", partition=partition
        )
    }
    pending = [r for r in data.season.rounds if r.status == "finished" and r.id not in processed]

    if not pending:
        logger.info(
            "biwenger delta %s %s: sin jornada nueva terminada; 0 detalles pedidos",
            competition,
            season,
        )
        result = IngestResult(rows={"fantasy_points": 0, "biwenger_prices": 0})
        result.stats = {REFRESHED: 0, SKIPPED: len(squad)}
        return result

    scorer_ids: set[int] = set()
    for round_meta in pending:
        round_data = client.fetch_round(competition, round_meta.id, 1).data
        scorer_ids |= _scoring_player_ids(round_data)

    to_refresh = [squad[player_id] for player_id in scorer_ids if player_id in squad]
    outside_squad = len(scorer_ids) - len(to_refresh)
    logger.info(
        "biwenger delta %s %s: %d jornada(s) nueva(s) %s, %d puntuaron (%d fuera de plantilla), "
        "refrescando %d",
        competition,
        season,
        len(pending),
        [r.id for r in pending],
        len(scorer_ids),
        outside_squad,
        len(to_refresh),
    )

    result = _refresh_players(
        client, storage, competition, season, to_refresh, batch_size=batch_size
    )
    result.stats = {REFRESHED: len(to_refresh), SKIPPED: len(squad) - len(to_refresh)}
    logger.info(
        "biwenger delta %s %s: %d refrescados, %d saltados, %d filas de puntos, %d fallidos",
        competition,
        season,
        result.stats[REFRESHED],
        result.stats[SKIPPED],
        result.rows["fantasy_points"],
        len(result.failures),
    )
    return result


# --- Rounds: puntos por jornada de todos los jugadores, sin sesgo (#51) ------
#
# El detalle por jugador solo cubre a los que siguen en la competición (los que
# se fueron dan 404). El endpoint de jornada, en cambio, lista a todos los que
# puntuaron esa jornada —incluidos los que ya no están—, así que da un histórico
# sin sesgo de supervivencia. Son cinco peticiones por jornada (una por sistema
# de puntuación) que se combinan en una fila por jugador-partido con los cinco
# sistemas como columnas, misma forma que ``fantasy_points`` pero sin minutos ni
# nota (eso lo aportan el detalle por jugador o SofaScore).

_ROUND_POINTS_COLUMNS = [
    "player_id",
    "team_id",
    "match_id",
    "round_id",
    *POINT_COLUMNS,
    "home",
    "home_score",
    "away_score",
    "result",
]


class RoundDiscoveryError(Exception):
    """No se pudo descubrir ninguna jornada de la temporada pedida.

    Ningún jugador de la plantilla actual sirvió detalle para esa temporada (los
    veteranos son la vía para sembrar el primer ``round_id``), así que no hay de
    dónde sacar el catálogo de jornadas.
    """


def _discover_seed_round(
    client: BiwengerClient,
    competition: str,
    season: str,
    players: Iterable[Player],
) -> int | None:
    """Primer ``round_id`` de la temporada, tomado del detalle de un veterano.

    Recorre la plantilla actual pidiendo el detalle de cada jugador para la
    temporada pedida. Un jugador que no jugó esa temporada da 404 (baja) o vuelve
    sin reports (fichaje posterior): se salta. El primero con reports aporta un
    ``round_id`` de la temporada; con él, la respuesta de la jornada trae el
    catálogo completo (``season.rounds``), sin lista manual. ``None`` si ninguno
    sirve.
    """
    for player in players:
        try:
            detail = client.fetch_player_reports(competition, player.slug, season).data
        except SourceHTTPError:
            continue
        for report in detail.reports:
            return report.match.round.id
    return None


def _accumulate_round(
    rows: dict[tuple[int, int], dict], round_data: RoundData, column: str
) -> None:
    """Vuelca en ``rows`` los puntos del sistema ``column`` de una jornada.

    Acumula la unión de jugadores de todas las peticiones (no solo la última): un
    sistema que la competición no publica (Segunda no da 5 ni 6) aporta menos
    filas, y esa columna queda nula sin perder a esos jugadores. La fila se crea
    la primera vez que un jugador-partido aparece, con sus datos de partido; las
    peticiones siguientes solo añaden su columna de puntos.
    """
    for game in round_data.games:
        for home, team in ((True, game.home), (False, game.away)):
            opponent = game.away if home else game.home
            for report in team.reports:
                key = (report.player.id, game.id)
                row = rows.get(key)
                if row is None:
                    row = {
                        "player_id": report.player.id,
                        "team_id": team.id,
                        "match_id": game.id,
                        "round_id": round_data.id,
                        "home": home,
                        "home_score": game.home.score,
                        "away_score": game.away.score,
                        "result": _match_result(team.score, opponent.score),
                    }
                    rows[key] = row
                row[column] = report.points


def _match_result(team_score: int | None, opponent_score: int | None) -> str | None:
    if team_score is None or opponent_score is None:
        return None
    if team_score > opponent_score:
        return "win"
    if team_score < opponent_score:
        return "loss"
    return "draw"


def _round_points_frame(rows: list[dict]) -> pd.DataFrame:
    nullable_ints = ["team_id", "match_id", "round_id", "home_score", "away_score", *POINT_COLUMNS]
    return pd.DataFrame(rows, columns=_ROUND_POINTS_COLUMNS).astype(
        dict.fromkeys(nullable_ints, "Int64")
    )


def ingest_rounds(
    storage: Storage,
    competition: str,
    season: str,
    *,
    transport: HttpTransport | None = None,
    round_ids: list[int] | None = None,
    resume: bool = False,
) -> IngestResult:
    """Publica fantasy_round_points de una temporada, jornada a jornada.

    Descubre las jornadas de la temporada (a menos que se pasen en ``round_ids``)
    sembrando un ``round_id`` desde el detalle de un veterano de la plantilla
    actual y leyendo el catálogo ``season.rounds`` de la respuesta de esa jornada.
    Para cada jornada hace cinco peticiones (una por sistema de puntuación) y
    vuelca una fila por jugador-partido con los cinco sistemas como columnas,
    incluidos los jugadores que ya dejaron la competición.

    El upsert por ``round_id`` hace la ingesta idempotente: reprocesar una jornada
    reescribe sus filas sin duplicar. Con ``resume=True`` se saltan las jornadas
    ya presentes en la tabla (temporada pasada inmutable), para no re-descargar en
    un backfill reanudado. Devuelve las filas escritas y el conteo de peticiones.
    """
    transport = transport or HttpTransport(
        wait_seconds=WAIT_SECONDS,
        overflow_proxy=scrapeops_proxy_from_env(enabled=PROXY_OVERFLOW),
    )
    client = BiwengerClient(transport, storage.raw)
    partition = {"competition": competition, "season": season}
    result = IngestResult(rows={"fantasy_round_points": 0})
    requests = 0

    if round_ids is None:
        players = client.fetch_competition_data(competition).data.players.values()
        seed = _discover_seed_round(client, competition, season, players)
        if seed is None:
            raise RoundDiscoveryError(
                f"Ningún jugador de la plantilla actual de {competition} sirvió detalle para la "
                f"temporada {season}: no hay de dónde descubrir las jornadas."
            )
        catalogue = client.fetch_round(competition, seed, 1).data
        requests += 1
        round_ids = [r.id for r in catalogue.season.rounds if r.status in (None, "finished")]
        logger.info(
            "biwenger rounds %s %s: %d jornadas descubiertas desde la jornada %d",
            competition,
            season,
            len(round_ids),
            seed,
        )

    already = (
        {
            int(v)
            for v in storage.curated.distinct_values(
                "fantasy_round_points", "round_id", partition=partition
            )
        }
        if resume
        else set()
    )

    for index, round_id in enumerate(round_ids, start=1):
        if round_id in already:
            logger.info(
                "biwenger rounds %s %s [%d/%d] jornada %d ya curada, saltada",
                competition,
                season,
                index,
                len(round_ids),
                round_id,
            )
            continue
        rows: dict[tuple[int, int], dict] = {}
        for system, column in POINT_SYSTEMS.items():
            round_data = client.fetch_round(competition, round_id, int(system)).data
            requests += 1
            _accumulate_round(rows, round_data, column)
        storage.curated.upsert_table(
            "fantasy_round_points",
            _round_points_frame(list(rows.values())),
            key="round_id",
            partition=partition,
        )
        result.rows["fantasy_round_points"] += len(rows)
        logger.info(
            "biwenger rounds %s %s [%d/%d] jornada %d: %d filas",
            competition,
            season,
            index,
            len(round_ids),
            round_id,
            len(rows),
        )

    logger.info(
        "biwenger rounds %s %s: %d filas, %d jornadas, %d peticiones",
        competition,
        season,
        result.rows["fantasy_round_points"],
        len(round_ids),
        requests,
    )
    return result
