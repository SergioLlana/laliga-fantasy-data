"""Transporte HTTP común a todas las fuentes.

Sesión curl-cffi con impersonación de Chrome (huella TLS y User-Agent
realistas, necesarios para SofaScore/FotMob), espera configurable entre
peticiones y reintentos con espera creciente ante 429/5xx.

Modo proxy opcional (ScrapeOps, Proxy Aggregator): apagado por defecto. Se
activa por fuente cuando `LFDATA_SCRAPEOPS_KEY` está definida y la fuente se
marca `proxy=true`; entonces las peticiones se enrutan por ScrapeOps, que rota
IPs y resuelve retos de Cloudflare. Ver docs/implementation/03.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Protocol
from urllib.parse import urlencode

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

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
    """Devuelve un proxy solo si la fuente lo pide y hay clave en el entorno.

    Sin `enabled` (fuente no marcada) o sin `LFDATA_SCRAPEOPS_KEY`, devuelve
    None: el transporte se comporta como si el proxy no existiera.
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

    return curl_requests.Session(impersonate="chrome")


class HttpTransport:
    """Cliente HTTP con ritmo y reintentos; una instancia por fuente."""

    def __init__(
        self,
        *,
        wait_seconds: float = 2.0,
        max_retries: int = 3,
        retry_wait_seconds: float = 5.0,
        session: _Session | None = None,
        proxy: ScrapeOpsProxy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._wait_seconds = wait_seconds
        self._max_retries = max_retries
        self._retry_wait_seconds = retry_wait_seconds
        self._session = session or _default_session()
        self._proxy = proxy
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None

    def get(self, url: str, params: Mapping[str, str | int] | None = None) -> bytes:
        request_url, request_params = url, params
        if self._proxy is not None:
            request_url, request_params = self._proxy.wrap(url, params)
        for attempt in range(self._max_retries + 1):
            self._wait_turn()
            response = self._session.get(request_url, params=request_params)
            if response.status_code == 200:
                return response.content
            if response.status_code in RETRYABLE_STATUSES and attempt < self._max_retries:
                self._sleep(self._retry_wait_seconds * 2**attempt)
                continue
            raise SourceHTTPError(url, response.status_code, proxy_active=self._proxy is not None)
        raise AssertionError("unreachable")

    def _wait_turn(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            remaining = self._wait_seconds - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()
