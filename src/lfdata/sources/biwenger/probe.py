"""Sonda de caracterización de la ventana de cuota de Biwenger (#54).

Biwenger corta con 429 sostenido a partir de la petición ~200 por ventana e IP;
la duración de esa ventana ("¿por hora?, ¿por día?") no está caracterizada (ADR
0004, docs/handoff-scraping.md §6.3). Esta sonda la mide: tras confirmar un 429
directo, lanza una petición ligera cada hora hasta recibir un 200 y registra
cuánto tardó la ventana en reponerse. Opcionalmente, tras la recuperación sigue
pidiendo para contar cuántas peticiones admite hasta el siguiente corte.

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

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lfdata.sources.biwenger.client import API_BASE, COMPETITIONS, WAIT_SECONDS
from lfdata.sources.http import BLOCK_STATUSES

logger = logging.getLogger(__name__)

# Petición de sondeo: la plantilla de la competición. Es el endpoint más robusto
# para "¿está la ventana abierta?" —no depende de un slug o round_id que pueda
# caducar— y una sola petición por hora hace irrelevante su tamaño. Cuenta contra
# la cuota igual que cualquier otra, que es lo único que la sonda necesita.
PROBE_PATH = "competitions/{competition}/data"
PROBE_PARAMS = {"lang": "es", "score": 1}

# Estado sintético cuando la petición ni siquiera llega a Biwenger (timeout o
# corte de red): no es 200 ni bloqueo, así que la sonda sigue esperando.
NETWORK_ERROR_STATUS = 0

DEFAULT_INTERVAL_SECONDS = 3600.0
DEFAULT_MAX_HOURS = 24.0
# Tope de la fase opcional de capacidad: por encima de ~200 la ventana ya debería
# haber cortado; el tope evita un bucle infinito si no lo hace.
DEFAULT_MAX_CAPACITY_REQUESTS = 300


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
) -> ProbeReport:
    """Sondea ``request`` cada ``interval_seconds`` hasta un 200 o el límite.

    ``request`` devuelve el código de estado de una petición directa (200, 429,
    403…); se le pasa desde fuera para que los tests inyecten una secuencia y la
    duración real no dependa del reloj. ``on_attempt`` se invoca tras cada intento
    con el informe acumulado, para volcar el registro de forma incremental (un
    corte a mitad deja los timestamps ya vistos en disco).

    Con ``measure_capacity``, tras el primer 200 sigue pidiendo (espaciando
    ``capacity_wait_seconds``) para contar cuántas peticiones admite la ventana
    recién repuesta antes del siguiente corte (opcional; quema la ventana).
    """
    now = now or (lambda: datetime.now(UTC))
    on_attempt = on_attempt or (lambda report, attempt: None)
    report = ProbeReport(started_at=now())
    deadline = report.started_at + timedelta(hours=max_hours)

    while True:
        attempt = _attempt(request, now)
        report.attempts.append(attempt)
        if attempt.status in BLOCK_STATUSES:
            if report.first_block_at is None:
                report.first_block_at = attempt.at
            report.last_block_at = attempt.at
        if attempt.status == 200:
            report.outcome = "recovered" if report.first_block_at else "already-open"
            report.recovered_at = attempt.at
            if report.outcome == "already-open":
                report.recovered_at = None
            on_attempt(report, attempt)
            break
        on_attempt(report, attempt)
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


def _probe_url(competition: str) -> str:
    return f"{API_BASE}/{PROBE_PATH.format(competition=competition)}"


def run_probe(
    competition: str,
    out_path: Path,
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    max_hours: float = DEFAULT_MAX_HOURS,
    measure_capacity: bool = False,
    session=None,
) -> ProbeReport:
    """Ejecuta la sonda contra Biwenger y deja el registro en ``out_path``.

    Construye una petición directa a la plantilla de ``competition`` y sondea la
    ventana. El registro se reescribe tras cada intento, de modo que un corte a
    mitad de las horas de espera conserva los timestamps ya vistos.
    """
    if competition not in COMPETITIONS:
        raise ValueError(f"Competición desconocida: {competition!r} (usa {COMPETITIONS})")
    session = session or _direct_session()
    url = _probe_url(competition)

    def request() -> int:
        return session.get(url, params=PROBE_PARAMS).status_code

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
        interval_seconds=interval_seconds,
        max_hours=max_hours,
        measure_capacity=measure_capacity,
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
