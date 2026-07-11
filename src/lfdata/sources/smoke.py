"""Sonda de humo: ¿aceptan Biwenger y SofaScore tráfico directo desde AWS? (#55).

Antes de automatizar el pipeline en Fargate hay que saber si las IPs públicas de
datacenter de AWS están vetadas por Biwenger o SofaScore: todo lo probado hasta
ahora salió de una IP residencial (docs/implementation/07 §orden de trabajo, punto
2; comentario de diseño en #24). Esta sonda lanza unas **decenas de peticiones
directas** (sin proxy) a cada fuente desde donde se ejecute —pensada para correr en
una tarea Fargate manual— y registra los códigos de respuesta. Con el reparto de
200/403/429 concluye, por fuente, si el acceso directo desde AWS es viable.

No se confunde con la sonda de cuota (``biwenger-quota``, #54): aquella mide cuánto
tarda en reponerse la ventana de 429 a lo largo de horas; ésta solo comprueba, en
un único disparo corto, qué recibe una IP de datacenter:

- **200** → el acceso directo es viable desde AWS.
- **403** → Cloudflare/WAF veta la IP de datacenter (el desbordamiento a proxy,
  ADR 0004, tendría que cubrir el hueco).
- **429** → cuota agotada, pero **no** veto: se repone o se rota IP (y cada tarea
  Fargate estrena IP pública, que además resetea la ventana de Biwenger).

Contrato (issue #55):

- Las peticiones van **siempre directas**, nunca por proxy: cada fuente usa una
  sesión directa que no conoce a ScrapeOps, así que el resultado refleja la IP real
  de la tarea, no una IP rotada que enmascararía el veto.
- No se escribe nada en ``curated/`` ni ``raw/``: solo deja un registro JSON con el
  código de cada petición y el veredicto por fuente.
- Corre desatendida y termina sola tras ``requests`` peticiones por fuente.

Biwenger se sondea con el **detalle por jugador** (endpoint no cacheable que
refleja el origen, igual que ``biwenger-quota``); SofaScore, con el listado de
temporadas por torneo (endpoint verificado, docs/implementation/03).
"""

from __future__ import annotations

import itertools
import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lfdata.sources.biwenger.client import API_BASE, COMPETITIONS
from lfdata.sources.biwenger.probe import (
    DEFAULT_SEASON,
    PLAYER_PATH,
    PROBE_FIELDS,
    RosterUnavailableError,
    _direct_session,
    _roster_slugs,
)

logger = logging.getLogger(__name__)

# Fuentes que valida la sonda; el pipeline diario pega a ambas directas.
SOURCES = ("biwenger", "sofascore")

# ~20-30 peticiones por fuente (issue #55): suficientes para ver el reparto de
# códigos sin acercarse al corte por cuota de Biwenger (~200 por ventana e IP), así
# que la sonda no quema la ventana de la tarea Fargate.
DEFAULT_REQUESTS = 25
# Mismo ritmo que un run real (WAIT_SECONDS de Biwenger): que la sonda se parezca al
# tráfico que validará, no a una ráfaga que dispare defensas antiabuso.
DEFAULT_WAIT_SECONDS = 2.0

# Estado sintético cuando la petición ni siquiera llega a la fuente (timeout o corte
# de red): no es 200 ni un veto, así que cuenta como intento fallido aparte.
NETWORK_ERROR_STATUS = 0

FORBIDDEN_STATUS = 403  # veto de IP / reto de Cloudflare-WAF a la IP de datacenter
RATE_LIMITED_STATUS = 429  # cuota agotada (no veto: se repone o rota IP)

# SofaScore: host y torneos verificados (docs/implementation/03). El listado de
# temporadas por torneo es un endpoint ligero y estable; rotar los ids da URLs
# distintas para que la tanda no se resuelva entera desde una única caché.
SOFASCORE_API_BASE = "https://api.sofascore.com/api/v1"
SOFASCORE_SEASONS_PATH = "unique-tournament/{tournament}/seasons"
# La Liga 8, LaLiga2 54, Premier 17, Serie A 23, Bundesliga 35, Ligue 1 34.
SOFASCORE_TOURNAMENTS = (8, 54, 17, 23, 35, 34)


@dataclass
class AccessAttempt:
    """Una petición directa de la sonda: cuándo, a qué URL y con qué estado."""

    at: datetime
    url: str
    status: int

    def to_dict(self) -> dict:
        return {"at": self.at.isoformat(), "url": self.url, "status": self.status}


@dataclass
class SourceAccessReport:
    """Resultado por fuente; se serializa al registro tras cada petición.

    ``verdict`` es uno de:

    - ``"viable"``: hubo al menos un 200 y ningún 403 — la IP de datacenter accede
      directo (puede convivir con algún 429 suelto de cuota).
    - ``"blocked"``: hubo algún 403 — Cloudflare/WAF veta la IP; hace falta el
      desbordamiento a proxy (ADR 0004).
    - ``"rate-limited"``: ni un 200 ni un 403, pero sí 429 — cuota agotada, no veto:
      viable espaciando o rotando IP (cada tarea Fargate estrena IP).
    - ``"unreachable"``: ni 200 ni 403 ni 429 (todo cortes de red u otros estados) —
      no concluyente; revisar el registro.
    """

    source: str
    started_at: datetime
    attempts: list[AccessAttempt] = field(default_factory=list)
    note: str | None = None

    @property
    def status_counts(self) -> dict[int, int]:
        """Cuántas veces salió cada código (200, 403, 429, 0…)."""
        return dict(Counter(attempt.status for attempt in self.attempts))

    @property
    def verdict(self) -> str:
        counts = self.status_counts
        if counts.get(FORBIDDEN_STATUS, 0):
            return "blocked"
        if counts.get(200, 0):
            return "viable"
        if counts.get(RATE_LIMITED_STATUS, 0):
            return "rate-limited"
        return "unreachable"

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "started_at": self.started_at.isoformat(),
            "verdict": self.verdict,
            "note": self.note,
            "status_counts": {str(status): n for status, n in sorted(self.status_counts.items())},
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }

    def summary(self) -> str:
        """Una línea legible por fuente para el log de cierre."""
        counts = ", ".join(f"{n}×{status}" for status, n in sorted(self.status_counts.items()))
        counts = counts or "sin peticiones"
        verdict_text = {
            "viable": "directo VIABLE desde datacenter",
            "blocked": "IP de datacenter VETADA (403 de Cloudflare/WAF)",
            "rate-limited": "cuota agotada, sin veto (429): viable espaciando/rotando IP",
            "unreachable": "no concluyente (sin 200/403/429)",
        }[self.verdict]
        line = f"{self.source}: {verdict_text} [{counts}]"
        if self.note:
            line += f" — {self.note}"
        return line


def probe_source_access(
    request: Callable[[], tuple[str, int]],
    *,
    source: str,
    count: int = DEFAULT_REQUESTS,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    on_attempt: Callable[[SourceAccessReport, AccessAttempt], None] | None = None,
) -> SourceAccessReport:
    """Lanza ``count`` peticiones directas de ``request`` y registra sus códigos.

    ``request`` devuelve ``(url, status)`` de una petición directa; se le pasa desde
    fuera para que los tests inyecten una secuencia sin tocar la red. Las builders
    reales capturan sus propios cortes de red y devuelven estado
    ``NETWORK_ERROR_STATUS`` (0), así que la sonda no aborta por un timeout suelto.
    ``on_attempt`` se invoca tras cada petición con el informe acumulado, para
    volcar el registro de forma incremental (un corte a mitad deja lo ya visto).
    """
    now = now or (lambda: datetime.now(UTC))
    on_attempt = on_attempt or (lambda report, attempt: None)
    report = SourceAccessReport(source=source, started_at=now())
    for index in range(count):
        if index:
            sleep(wait_seconds)
        url, status = request()
        attempt = AccessAttempt(at=now(), url=url, status=status)
        report.attempts.append(attempt)
        on_attempt(report, attempt)
    return report


# --- Builders de peticiones por fuente ----------------------------------------


def _biwenger_request(session, competition: str, season: str, slugs: list[str]):
    """Sondea el detalle por jugador rotando slugs (URL nueva = refleja el origen).

    Igual que ``biwenger-quota``: la plantilla la sirve Cloudflare desde caché y
    daría 200 aunque el origen esté a 429, así que se usa el detalle por jugador,
    que Cloudflare no cachea. Los cortes de red se traducen a estado 0 para no
    abortar la tanda.
    """
    slug_cycle = itertools.cycle(slugs)

    def request() -> tuple[str, int]:
        slug = next(slug_cycle)
        url = f"{API_BASE}/{PLAYER_PATH.format(competition=competition, slug=slug)}"
        return url, _status(
            lambda: session.get(url, params={"fields": PROBE_FIELDS, "season": season}), "biwenger"
        )

    return request


def _sofascore_request(session):
    """Sondea el listado de temporadas rotando torneos (URLs distintas)."""
    tournament_cycle = itertools.cycle(SOFASCORE_TOURNAMENTS)

    def request() -> tuple[str, int]:
        tournament = next(tournament_cycle)
        url = f"{SOFASCORE_API_BASE}/{SOFASCORE_SEASONS_PATH.format(tournament=tournament)}"
        return url, _status(lambda: session.get(url), "sofascore")

    return request


def _status(call: Callable[[], object], source: str) -> int:
    """Ejecuta la petición y devuelve su código; un corte de red da estado 0."""
    try:
        return call().status_code  # type: ignore[attr-defined]
    except Exception as error:  # noqa: BLE001 — desatendida: ningún fallo puntual la mata
        logger.warning(
            "sonda de humo %s: petición fallida (%s), cuenta como corte de red", source, error
        )
        return NETWORK_ERROR_STATUS


# --- Runner: sesiones directas reales y volcado del registro ------------------


def run_smoke(
    out_path: Path,
    *,
    sources: tuple[str, ...] = SOURCES,
    competition: str = "la-liga",
    season: str = DEFAULT_SEASON,
    count: int = DEFAULT_REQUESTS,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    biwenger_session=None,
    sofascore_session=None,
    slugs: list[str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, SourceAccessReport]:
    """Ejecuta la sonda de humo contra cada fuente y deja el registro en ``out_path``.

    Cada fuente usa su propia sesión directa (curl-cffi impersonando Chrome, sin
    proxy). El registro combinado se reescribe tras cada petición, así un corte a
    mitad conserva lo ya sondeado.
    """
    unknown = set(sources) - set(SOURCES)
    if unknown:
        raise ValueError(f"Fuentes desconocidas: {sorted(unknown)} (usa {SOURCES})")
    if competition not in COMPETITIONS:
        raise ValueError(f"Competición desconocida: {competition!r} (usa {COMPETITIONS})")

    started = datetime.now(UTC)
    reports: dict[str, SourceAccessReport] = {}

    def on_attempt(report: SourceAccessReport, attempt: AccessAttempt) -> None:
        logger.info(
            "sonda de humo %s [%d/%d] HTTP %d",
            report.source,
            len(report.attempts),
            count,
            attempt.status,
        )
        _write_record(out_path, started, reports)

    for source in sources:
        if source == "biwenger":
            session = biwenger_session or _direct_session()
            try:
                source_slugs = slugs if slugs is not None else _roster_slugs(session, competition)
            except RosterUnavailableError as error:
                reports[source] = SourceAccessReport(
                    source=source,
                    started_at=datetime.now(UTC),
                    note=f"no se pudo obtener la plantilla para sacar slugs: {error}",
                )
                logger.warning("sonda de humo biwenger: %s", error)
                _write_record(out_path, started, reports)
                continue
            request = _biwenger_request(session, competition, season, source_slugs)
        else:
            session = sofascore_session or _direct_session()
            request = _sofascore_request(session)

        logger.info(
            "sonda de humo %s: %d peticiones directas cada %.0f s", source, count, wait_seconds
        )
        reports[source] = probe_source_access(
            request,
            source=source,
            count=count,
            wait_seconds=wait_seconds,
            sleep=sleep,
            on_attempt=on_attempt,
        )

    _write_record(out_path, started, reports)
    for report in reports.values():
        logger.info("sonda de humo: %s", report.summary())
    return reports


def _write_record(
    out_path: Path, started: datetime, reports: dict[str, SourceAccessReport]
) -> None:
    """Vuelca el registro combinado a ``out_path`` de forma atómica (rename)."""
    payload = {
        "kind": "direct-access-smoke",
        "started_at": started.isoformat(),
        "sources": {source: report.to_dict() for source, report in reports.items()},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out_path)


def default_out_path(*, started: datetime | None = None) -> Path:
    """Nombre de registro con timestamp para no pisar ejecuciones previas."""
    started = started or datetime.now(UTC)
    return Path(f"direct-access-smoke-{started:%Y%m%dT%H%M%SZ}.json")
