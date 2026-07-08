"""Transporte HTTP común a todas las fuentes.

Sesión curl-cffi con impersonación de Chrome (huella TLS y User-Agent
realistas, necesarios para SofaScore/FotMob), espera configurable entre
peticiones y reintentos con espera creciente ante 429/5xx.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Protocol

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class SourceHTTPError(Exception):
    """La fuente respondió con un estado que no sabemos manejar."""

    def __init__(self, url: str, status: int) -> None:
        super().__init__(f"HTTP {status} al pedir {url}")
        self.url = url
        self.status = status


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
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._wait_seconds = wait_seconds
        self._max_retries = max_retries
        self._retry_wait_seconds = retry_wait_seconds
        self._session = session or _default_session()
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None

    def get(self, url: str, params: Mapping[str, str | int] | None = None) -> bytes:
        for attempt in range(self._max_retries + 1):
            self._wait_turn()
            response = self._session.get(url, params=params)
            if response.status_code == 200:
                return response.content
            if response.status_code in RETRYABLE_STATUSES and attempt < self._max_retries:
                self._sleep(self._retry_wait_seconds * 2**attempt)
                continue
            raise SourceHTTPError(url, response.status_code)
        raise AssertionError("unreachable")

    def _wait_turn(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            remaining = self._wait_seconds - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()
