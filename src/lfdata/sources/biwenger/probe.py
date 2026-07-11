"""Sonda de caracterización de la ventana de cuota de Biwenger (#54).

Biwenger corta con 429 sostenido a partir de la petición ~200 por ventana e IP;
la duración de esa ventana ("¿por hora?, ¿por día?") no está caracterizada (ADR
0004, docs/handoff-scraping.md §6.3). Esta sonda la mide: tras confirmar un 429
directo, lanza una petición ligera cada hora hasta recibir un 200 sostenido y
registra cuánto tardó la ventana en reponerse. Opcionalmente, tras la recuperación
sigue pidiendo para contar cuántas peticiones admite hasta el siguiente corte.

Sondea el **detalle por jugador** (``players/{competition}/{slug}``, con
``fields=id`` y un **parámetro cache-buster único por petición**): es el endpoint
que el backfill usa, y el cache-buster garantiza que Cloudflare lo sirva siempre
desde el origen (``cf-cache-status: BYPASS``), así que su 200/429 refleja la cuota
real. Sin el cache-buster NO sirve: Cloudflare cachea el detalle **por slug** y
devuelve un 200 desde caché aunque el origen esté a 429 (verificado el 2026-07-11:
la sonda veía 200 en los primeros slugs del roster —cacheados— mientras el backfill
recibía 429 a la vez). La plantilla (``competitions/.../data``) tiene el mismo
problema de caché y por eso tampoco vale como sonda.

El resultado decide si el post-jornada puede ir en tandas directas espaciadas
(0 créditos) o necesita el desbordamiento a proxy.

Contrato (issue #54):

- Las peticiones de sondeo van **siempre directas**, nunca por proxy: la sonda
  usa una sesión directa y no conoce a ScrapeOps, así que medir la ventana real
  no la enmascara detrás de IPs rotadas.
- No se escribe nada en ``curated/`` (ni ``raw/``): la sonda solo deja un
  registro JSON con los timestamps de 429→200 y la duración estimada.
- Corre desatendida y termina sola al primer 200, o al agotar un límite de horas
  configurable.
"""

from __future__ import annotations

import itertools
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lfdata.sources.biwenger.client import API_BASE, COMPETITIONS, WAIT_SECONDS
from lfdata.sources.http import BLOCK_STATUSES

logger = logging.getLogger(__name__)

# Petición de sondeo: el detalle por jugador (players/{competition}/{slug}), que es
# el endpoint que usa el backfill. CLAVE para que refleje la cuota real: Cloudflare
# SÍ cachea el detalle por slug (verificado el 2026-07-11: los primeros slugs del
# roster daban cf-cache-status=HIT y 200 aunque el origen estuviera a 429), así que
# rotar entre slugs NO basta —los cacheados enmascaran el 429—. Lo que fuerza el
# origen es el parámetro cache-buster único por petición (CACHE_BUSTER_PARAM): al
# cambiar la URL en cada sondeo, Cloudflare no encuentra entrada en caché, hace
# BYPASS al origen y devuelve el 200/429 real (que además cuenta contra la cuota,
# justo lo que la sonda mide). El roster se pide solo al arrancar, para obtener un
# slug válido con el que formar la URL.
PLAYER_PATH = "players/{competition}/{slug}"
ROSTER_PATH = "competitions/{competition}/data"
# Payload mínimo (fields=id): la URL sigue contando contra la cuota, que es lo único
# que la sonda mide; no necesita reports ni precios.
PROBE_FIELDS = "id"
# Nombre del parámetro cache-buster. Se rellena con un valor único (uuid) por
# petición para que Cloudflare nunca sirva el detalle desde caché y todo sondeo
# llegue al origen. Biwenger ignora parámetros desconocidos, así que no altera la
# respuesta (verificado el 2026-07-11: con buster el mismo slug pasa de HIT/200 a
# BYPASS/429).
CACHE_BUSTER_PARAM = "_"
DEFAULT_SEASON = "2026"

# Estado sintético cuando la petición ni siquiera llega a Biwenger (timeout o
# corte de red): no es 200 ni bloqueo, así que la sonda sigue esperando.
NETWORK_ERROR_STATUS = 0

DEFAULT_INTERVAL_SECONDS = 3600.0
DEFAULT_MAX_HOURS = 24.0
# Tope de la fase opcional de capacidad: por encima de ~200 la ventana ya debería
# haber cortado; el tope evita un bucle infinito si no lo hace.
DEFAULT_MAX_CAPACITY_REQUESTS = 300

# La cuota de Biwenger no se abre de golpe: es un rate-limiter que, tras el corte,
# deja colar alguna petición suelta (un 200 aislado) minutos antes de reponerse de
# verdad. Para no declarar "repuesta" por ese *blip*, al primer 200 la sonda pide
# unas cuantas más seguidas y solo lo da por bueno si TODAS pasan. Verificado el
# 2026-07-11: tras 429 en la petición ~201, peticiones sueltas devolvían 200 a los
# ~2 min pero volvían a 429 enseguida.
DEFAULT_CONFIRM_REQUESTS = 3
DEFAULT_CONFIRM_WAIT_SECONDS = 5.0


@dataclass
class ProbeAttempt:
    """Una petición de sondeo: cuándo se lanzó y con qué estado respondió."""

    at: datetime
    status: int

    def to_dict(self) -> dict:
        return {"at": self.at.isoformat(), "status": self.status}


@dataclass
class ProbeReport:
    """Resultado acumulado de la sonda; se serializa al registro tras cada intento.

    ``outcome`` es uno de:

    - ``"recovered"``: se vio el 429 y luego un 200 — la ventana se repuso y
      ``window_seconds`` la acota.
    - ``"already-open"``: el primer sondeo ya dio 200 — la ventana se repuso antes
      de empezar a medir, así que no hay duración que estimar.
    - ``"timed-out"``: se agotó ``max_hours`` sin ver un 200 — la ventana dura más
      que el límite (o la IP sigue vetada).
    """

    started_at: datetime
    attempts: list[ProbeAttempt] = field(default_factory=list)
    first_block_at: datetime | None = None
    last_block_at: datetime | None = None
    recovered_at: datetime | None = None
    outcome: str = "timed-out"
    capacity_requests: int | None = None

    @property
    def window_seconds(self) -> float | None:
        """Cota superior de la ventana: del primer 429 al 200 observado.

        La recuperación real cayó en algún punto del último intervalo (entre el
        último 429 y este 200), así que este valor sobreestima como mucho en un
        ``interval`` la duración real. ``recovery_lower_seconds`` da la cota
        inferior.
        """
        if self.first_block_at is None or self.recovered_at is None:
            return None
        return (self.recovered_at - self.first_block_at).total_seconds()

    @property
    def recovery_lower_seconds(self) -> float | None:
        """Cota inferior de la ventana: del primer 429 al último 429 observado."""
        if self.first_block_at is None or self.last_block_at is None:
            return None
        return (self.last_block_at - self.first_block_at).total_seconds()

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "outcome": self.outcome,
            "first_block_at": self.first_block_at.isoformat() if self.first_block_at else None,
            "last_block_at": self.last_block_at.isoformat() if self.last_block_at else None,
            "recovered_at": self.recovered_at.isoformat() if self.recovered_at else None,
            "window_seconds": self.window_seconds,
            "recovery_lower_seconds": self.recovery_lower_seconds,
            "capacity_requests": self.capacity_requests,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }

    def summary(self) -> str:
        """Una línea legible para el log de cierre."""
        if self.outcome == "recovered":
            window = self.window_seconds or 0.0
            lower = self.recovery_lower_seconds or 0.0
            text = (
                f"ventana repuesta: {_format_duration(lower)}–{_format_duration(window)} "
                f"(primer 429 {_hm(self.first_block_at)} → 200 {_hm(self.recovered_at)})"
            )
            if self.capacity_requests is not None:
                text += f"; admite ~{self.capacity_requests} peticiones hasta el siguiente corte"
            return text
        if self.outcome == "already-open":
            return "la ventana ya estaba abierta al empezar: primer sondeo dio 200, nada que medir"
        return (
            f"sin 200 tras {len(self.attempts)} intentos: la ventana dura más que el límite "
            "o la IP sigue vetada"
        )


def probe_quota_window(
    request: Callable[[], int],
    *,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    max_hours: float = DEFAULT_MAX_HOURS,
    on_attempt: Callable[[ProbeReport, ProbeAttempt], None] | None = None,
    measure_capacity: bool = False,
    capacity_wait_seconds: float = WAIT_SECONDS,
    max_capacity_requests: int = DEFAULT_MAX_CAPACITY_REQUESTS,
    confirm_requests: int = DEFAULT_CONFIRM_REQUESTS,
    confirm_wait_seconds: float = DEFAULT_CONFIRM_WAIT_SECONDS,
) -> ProbeReport:
    """Sondea ``request`` cada ``interval_seconds`` hasta un 200 sostenido o el límite.

    ``request`` devuelve el código de estado de una petición directa (200, 429,
    403…); se le pasa desde fuera para que los tests inyecten una secuencia y la
    duración real no dependa del reloj. ``on_attempt`` se invoca tras cada intento
    con el informe acumulado, para volcar el registro de forma incremental (un
    corte a mitad deja los timestamps ya vistos en disco).

    Como la cuota es un rate-limiter que oscila, un 200 aislado no basta: tras el
    primer 200 posterior a un bloqueo, la sonda pide ``confirm_requests`` en total
    (espaciando ``confirm_wait_seconds``) y solo declara la ventana repuesta si
    todas pasan. Si alguna vuelve a bloquear, era un *blip*: se registra y la sonda
    sigue esperando al siguiente intervalo.

    Con ``measure_capacity``, tras confirmar la recuperación sigue pidiendo
    (espaciando ``capacity_wait_seconds``) para contar cuántas peticiones admite la
    ventana antes del siguiente corte (opcional; quema la ventana).
    """
    now = now or (lambda: datetime.now(UTC))
    on_attempt = on_attempt or (lambda report, attempt: None)
    report = ProbeReport(started_at=now())
    deadline = report.started_at + timedelta(hours=max_hours)

    while True:
        attempt = _attempt(request, now)
        report.attempts.append(attempt)
        _note_block(report, attempt)
        on_attempt(report, attempt)
        if attempt.status == 200:
            if report.first_block_at is None:
                report.outcome = "already-open"
                break
            if _confirm_recovery(
                request,
                now=now,
                sleep=sleep,
                report=report,
                on_attempt=on_attempt,
                confirm_requests=confirm_requests,
                wait_seconds=confirm_wait_seconds,
            ):
                report.outcome = "recovered"
                report.recovered_at = attempt.at
                break
            # Fue un blip: la ventana sigue oscilando. Volver a esperar un intervalo.
        if now() + timedelta(seconds=interval_seconds) > deadline:
            report.outcome = "timed-out"
            break
        sleep(interval_seconds)

    if report.outcome == "recovered" and measure_capacity:
        report.capacity_requests = _measure_capacity(
            request,
            now=now,
            sleep=sleep,
            wait_seconds=capacity_wait_seconds,
            max_requests=max_capacity_requests,
            on_attempt=on_attempt,
            report=report,
        )
    return report


def _attempt(request: Callable[[], int], now: Callable[[], datetime]) -> ProbeAttempt:
    """Un intento tolerante a fallos: un corte de red no aborta la sonda."""
    try:
        status = request()
    except Exception as error:  # noqa: BLE001 — desatendida: ningún fallo puntual la mata
        logger.warning(
            "sonda biwenger: petición fallida (%s), se reintenta al siguiente turno", error
        )
        status = NETWORK_ERROR_STATUS
    return ProbeAttempt(at=now(), status=status)


def _note_block(report: ProbeReport, attempt: ProbeAttempt) -> None:
    """Actualiza las marcas del bloqueo si el intento fue un 429/403."""
    if attempt.status in BLOCK_STATUSES:
        if report.first_block_at is None:
            report.first_block_at = attempt.at
        report.last_block_at = attempt.at


def _confirm_recovery(
    request: Callable[[], int],
    *,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    report: ProbeReport,
    on_attempt: Callable[[ProbeReport, ProbeAttempt], None],
    confirm_requests: int,
    wait_seconds: float,
) -> bool:
    """¿Es sostenido el 200 recién visto, o un blip del rate-limiter?

    El 200 que dispara la confirmación ya cuenta como el primero, así que pide
    ``confirm_requests - 1`` más (espaciando ``wait_seconds``). Devuelve ``True``
    solo si todas pasan; al primer no-200 (bloqueo o corte) devuelve ``False`` y
    la sonda vuelve a esperar un intervalo.
    """
    for _ in range(max(confirm_requests - 1, 0)):
        sleep(wait_seconds)
        attempt = _attempt(request, now)
        report.attempts.append(attempt)
        _note_block(report, attempt)
        on_attempt(report, attempt)
        if attempt.status != 200:
            return False
    return True


def _measure_capacity(
    request: Callable[[], int],
    *,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    wait_seconds: float,
    max_requests: int,
    on_attempt: Callable[[ProbeReport, ProbeAttempt], None],
    report: ProbeReport,
) -> int:
    """Cuenta las peticiones admitidas hasta el siguiente bloqueo.

    El 200 que abrió la ventana ya consumió una petición, así que el conteo
    empieza en 1. Espacia ``wait_seconds`` como un run real y para al primer
    bloqueo o al tope ``max_requests``.
    """
    admitted = 1
    while admitted < max_requests:
        sleep(wait_seconds)
        attempt = _attempt(request, now)
        report.attempts.append(attempt)
        on_attempt(report, attempt)
        if attempt.status in BLOCK_STATUSES:
            break
        if attempt.status == 200:
            admitted += 1
    return admitted


# --- Runner: sesión directa real y volcado del registro -----------------------


def _direct_session():
    """Sesión curl-cffi directa (impersona Chrome), sin proxy alguno."""
    from curl_cffi import requests as curl_requests

    return curl_requests.Session(impersonate="chrome", timeout=30.0)


class RosterUnavailableError(Exception):
    """No se pudo obtener la lista de slugs con la que sondear."""


def _roster_slugs(session, competition: str, *, retries: int = 3, sleep=time.sleep) -> list[str]:
    """Slugs de la plantilla actual, para rotar entre ellos al sondear.

    La plantilla la sirve Cloudflare desde caché, así que suele dar 200 incluso con
    el origen a 429; aun así se reintenta por si toca un cache-miss. Sin plantilla
    no hay con qué sondear: se falla claro.
    """
    url = f"{API_BASE}/{ROSTER_PATH.format(competition=competition)}"
    for attempt in range(retries):
        response = session.get(url, params={"lang": "es", "score": 1})
        if response.status_code == 200:
            players = json.loads(response.content)["data"]["players"]
            return [player["slug"] for player in players.values()]
        logger.warning(
            "sonda biwenger %s: plantilla dio HTTP %d al pedir slugs (intento %d/%d)",
            competition,
            response.status_code,
            attempt + 1,
            retries,
        )
        sleep(5.0 * (attempt + 1))
    raise RosterUnavailableError(
        f"No se pudo obtener la plantilla de {competition} para sacar slugs de sondeo "
        "(la caché de Cloudflare debería servirla; reintenta en un momento)."
    )


def run_probe(
    competition: str,
    out_path: Path,
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    max_hours: float = DEFAULT_MAX_HOURS,
    measure_capacity: bool = False,
    confirm_requests: int = DEFAULT_CONFIRM_REQUESTS,
    season: str = DEFAULT_SEASON,
    slugs: list[str] | None = None,
    session=None,
    sleep: Callable[[float], None] = time.sleep,
) -> ProbeReport:
    """Ejecuta la sonda contra Biwenger y deja el registro en ``out_path``.

    Sondea el detalle por jugador (endpoint no cacheable, refleja la cuota real)
    rotando entre los slugs de la plantilla, de modo que cada sondeo es una URL
    nueva. El registro se reescribe tras cada intento, así un corte a mitad de las
    horas de espera conserva los timestamps ya vistos.
    """
    if competition not in COMPETITIONS:
        raise ValueError(f"Competición desconocida: {competition!r} (usa {COMPETITIONS})")
    session = session or _direct_session()
    # Los slugs se resuelven de forma PEREZOSA dentro del bucle, no al arrancar. Si la
    # plantilla aún da 429 (caché de Cloudflare fría), resolverla aquí mataría una
    # sonda pensada justamente para esperar a que el 429 se reponga: el fallo de
    # plantilla lo captura _attempt como un turno perdido (igual que un corte de red)
    # y se reintenta al siguiente intervalo, hasta que la plantilla vuelva a servirse.
    slug_cycle = itertools.cycle(slugs) if slugs is not None else None

    def request() -> int:
        nonlocal slug_cycle
        if slug_cycle is None:
            slug_cycle = itertools.cycle(_roster_slugs(session, competition, sleep=sleep))
        slug = next(slug_cycle)
        url = f"{API_BASE}/{PLAYER_PATH.format(competition=competition, slug=slug)}"
        # El cache-buster (uuid único) fuerza un BYPASS al origen: sin él Cloudflare
        # serviría el detalle desde caché con un 200 stale y la sonda no vería el 429.
        params = {
            "fields": PROBE_FIELDS,
            "season": season,
            CACHE_BUSTER_PARAM: uuid.uuid4().hex,
        }
        return session.get(url, params=params).status_code

    def on_attempt(report: ProbeReport, attempt: ProbeAttempt) -> None:
        logger.info(
            "sonda biwenger %s [intento %d] %s HTTP %d",
            competition,
            len(report.attempts),
            _hm(attempt.at),
            attempt.status,
        )
        _write_report(out_path, competition, report)

    logger.info(
        "sonda biwenger %s: sondeando cada %s hasta 200 (límite %.0f h); registro en %s",
        competition,
        _format_duration(interval_seconds),
        max_hours,
        out_path,
    )
    report = probe_quota_window(
        request,
        sleep=sleep,
        interval_seconds=interval_seconds,
        max_hours=max_hours,
        measure_capacity=measure_capacity,
        confirm_requests=confirm_requests,
        on_attempt=on_attempt,
    )
    _write_report(out_path, competition, report)
    logger.info("sonda biwenger %s: %s", competition, report.summary())
    return report


def _write_report(out_path: Path, competition: str, report: ProbeReport) -> None:
    """Vuelca el informe a ``out_path`` de forma atómica (rename)."""
    payload = {"source": "biwenger", "competition": competition, **report.to_dict()}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out_path)


def default_out_path(competition: str, *, started: datetime | None = None) -> Path:
    """Nombre de registro con timestamp para no pisar ejecuciones previas."""
    started = started or datetime.now(UTC)
    return Path(f"biwenger-quota-probe-{competition}-{started:%Y%m%dT%H%M%SZ}.json")


def _hm(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M") if moment else "—"


def _format_duration(seconds: float) -> str:
    """Segundos a un texto humano compacto (``2 h 30 min``, ``45 min``, ``30 s``)."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min" if secs == 0 else f"{minutes} min {secs} s"
    hours, mins = divmod(minutes, 60)
    return f"{hours} h" if mins == 0 else f"{hours} h {mins} min"
