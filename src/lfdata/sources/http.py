"""Transporte HTTP común a todas las fuentes.

Sesión curl-cffi con impersonación de Chrome (huella TLS y User-Agent
realistas, necesarios para SofaScore/FotMob), espera configurable entre
peticiones y reintentos con espera creciente ante 429/5xx.

Proxy ScrapeOps como **desbordamiento**, no como vía principal (ADR 0004): por
defecto toda petición va directa (gratis). Si la fuente se marca con
desbordamiento permitido y hay `LFDATA_SCRAPEOPS_KEY` en el entorno, el
transporte conmuta a ScrapeOps —que rota IPs y resuelve retos de Cloudflare—
solo cuando la fuente confirma un bloqueo persistente (429/403 tras el primer
reintento). A partir de la conmutación, el resto del run de esa fuente va por
proxy sin re-probar la vía directa. Sin clave, el comportamiento es el de
siempre: directo con reintentos. Ver docs/adr/0004 y docs/implementation/03.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Protocol
from urllib.parse import urlencode

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Estados que delatan un bloqueo por IP/cuota (no un fallo transitorio de red):
# 429 (cuota agotada) y 403 (reto de Cloudflare / IP vetada). Si hay proxy de
# desbordamiento disponible, gatillan la conmutación en vez de quemar el backoff
# directo contra una IP cuya ventana tarda horas en reponerse.
BLOCK_STATUSES = frozenset({403, 429})

# Por el proxy las peticiones tardan bastante más (la plantilla de la-liga son
# ~37 s: payload grande + salto ScrapeOps), así que el timeout por defecto de
# 30 s de curl no basta. 90 s deja margen sin colgarse indefinidamente.
REQUEST_TIMEOUT_SECONDS = 90.0

SCRAPEOPS_ENDPOINT = "https://proxy.scrapeops.io/v1/"
SCRAPEOPS_KEY_ENV = "LFDATA_SCRAPEOPS_KEY"


class SourceHTTPError(Exception):
    """La fuente respondió con un estado que no sabemos manejar."""

    def __init__(self, url: str, status: int, *, proxy_active: bool = False) -> None:
        message = f"HTTP {status} al pedir {url}"
        if status == 403 and not proxy_active:
            message += (
                f". Posible bloqueo por IP/Cloudflare: define {SCRAPEOPS_KEY_ENV} y marca "
                "la fuente con proxy=true para enrutar por ScrapeOps."
            )
        super().__init__(message)
        self.url = url
        self.status = status


class ScrapeOpsProxy:
    """Enruta las peticiones por el Proxy Aggregator de ScrapeOps.

    ScrapeOps toma la URL de destino (con su query string ya incrustada) como
    parámetro `url` y la clave como `api_key`; devuelve la respuesta del destino.
    """

    def __init__(self, api_key: str, *, endpoint: str = SCRAPEOPS_ENDPOINT) -> None:
        self._api_key = api_key
        self._endpoint = endpoint

    def wrap(self, url: str, params: Mapping[str, str | int] | None) -> tuple[str, dict[str, str]]:
        target = url
        if params:
            target = f"{url}?{urlencode(dict(params))}"
        return self._endpoint, {"api_key": self._api_key, "url": target}


def scrapeops_proxy_from_env(
    *, enabled: bool, env: Mapping[str, str] | None = None
) -> ScrapeOpsProxy | None:
    """Devuelve el proxy de desbordamiento si la fuente lo permite y hay clave.

    Sin `enabled` (fuente sin desbordamiento) o sin `LFDATA_SCRAPEOPS_KEY`,
    devuelve None: el transporte va siempre directo, como si el proxy no
    existiera. Con ambos, devuelve un proxy que el transporte solo usará tras
    confirmar un bloqueo persistente.
    """
    if not enabled:
        return None
    env = env if env is not None else os.environ
    key = env.get(SCRAPEOPS_KEY_ENV)
    if not key:
        return None
    return ScrapeOpsProxy(key)


class _Response(Protocol):
    status_code: int
    content: bytes


class _Session(Protocol):
    def get(self, url: str, params: Mapping[str, str | int] | None = None) -> _Response: ...


def _default_session() -> _Session:
    from curl_cffi import requests as curl_requests

    return curl_requests.Session(impersonate="chrome", timeout=REQUEST_TIMEOUT_SECONDS)


def _default_retryable_exceptions() -> tuple[type[BaseException], ...]:
    """Errores de transporte que merecen reintento (timeout, corte de conexión).

    Import perezoso: solo el backend real (curl-cffi) los produce; los tests
    inyectan sus propias excepciones. Un timeout aislado a través del proxy es
    transitorio —conviene reintentar, no abortar el run entero por él—.
    """
    from curl_cffi.requests.exceptions import RequestException

    return (RequestException,)


class HttpTransport:
    """Cliente HTTP con ritmo y reintentos; una instancia por fuente."""

    def __init__(
        self,
        *,
        wait_seconds: float = 2.0,
        max_retries: int = 3,
        retry_wait_seconds: float = 5.0,
        session: _Session | None = None,
        overflow_proxy: ScrapeOpsProxy | None = None,
        retryable_exceptions: tuple[type[BaseException], ...] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._wait_seconds = wait_seconds
        self._max_retries = max_retries
        self._retry_wait_seconds = retry_wait_seconds
        self._session = session or _default_session()
        self._overflow_proxy = overflow_proxy
        self._overflow_active = False
        self._retryable_exceptions = (
            retryable_exceptions
            if retryable_exceptions is not None
            else _default_retryable_exceptions()
        )
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None

    def get(self, url: str, params: Mapping[str, str | int] | None = None) -> bytes:
        for attempt in range(self._max_retries + 1):
            self._wait_turn()
            request_url, request_params = self._route(url, params)
            try:
                response = self._session.get(request_url, params=request_params)
            except self._retryable_exceptions as error:
                # Timeout o corte de red: reintenta con espera creciente. Agotados
                # los intentos, se traduce a un 504 para que la fuente lo trate
                # como cualquier otro fallo (p. ej. saltar el jugador), no crashee.
                if attempt < self._max_retries:
                    self._sleep(self._retry_wait_seconds * 2**attempt)
                    continue
                raise SourceHTTPError(url, 504, proxy_active=self._overflow_active) from error
            if response.status_code == 200:
                return response.content
            status = response.status_code
            # Desbordamiento (ADR 0004): un bloqueo por IP/cuota que persiste tras
            # el primer reintento directo confirma que la ventana está agotada.
            # Conmutar a proxy y reintentar ya por IP rotada, sin quemar el resto
            # del backoff (5-10-20 s) contra una IP muerta. El primer 429/403 sí
            # se reintenta directo, por si fuera un blip transitorio.
            if self._can_overflow() and status in BLOCK_STATUSES and attempt < self._max_retries:
                if attempt >= 1:
                    self._overflow_active = True
                    continue
                self._sleep(self._retry_wait_seconds * 2**attempt)
                continue
            if status in RETRYABLE_STATUSES and attempt < self._max_retries:
                self._sleep(self._retry_wait_seconds * 2**attempt)
                continue
            raise SourceHTTPError(url, status, proxy_active=self._overflow_active)
        raise AssertionError("unreachable")

    def _can_overflow(self) -> bool:
        """Hay proxy de desbordamiento disponible y aún no hemos conmutado."""
        return self._overflow_proxy is not None and not self._overflow_active

    def _route(
        self, url: str, params: Mapping[str, str | int] | None
    ) -> tuple[str, Mapping[str, str | int] | None]:
        """URL y params efectivos: por proxy solo tras conmutar, directo si no."""
        if self._overflow_active and self._overflow_proxy is not None:
            return self._overflow_proxy.wrap(url, params)
        return url, params

    def _wait_turn(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            remaining = self._wait_seconds - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()
