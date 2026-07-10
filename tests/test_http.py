from dataclasses import dataclass, field

import pytest

from lfdata.sources.http import (
    SCRAPEOPS_ENDPOINT,
    HttpTransport,
    ScrapeOpsProxy,
    SourceHTTPError,
    scrapeops_proxy_from_env,
)


@dataclass
class FakeResponse:
    status_code: int
    content: bytes = b"ok"


@dataclass
class FakeCall:
    url: str
    params: dict | None


class FakeTimeout(Exception):
    """Doble del timeout de transporte de curl-cffi para los tests."""


@dataclass
class FakeSession:
    responses: list[FakeResponse]
    calls: list[FakeCall] = field(default_factory=list)

    def get(self, url, params=None):
        self.calls.append(FakeCall(url, dict(params) if params else None))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClock:
    """Reloj que avanza solo cuando se duerme."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_transport(responses: list[FakeResponse], **kwargs) -> tuple[HttpTransport, FakeClock]:
    clock = FakeClock()
    session = FakeSession(responses)
    transport = HttpTransport(session=session, sleep=clock.sleep, clock=clock, **kwargs)
    return transport, clock


def test_returns_content_on_200() -> None:
    transport, _ = make_transport([FakeResponse(200, b"body")])
    assert transport.get("https://x/a") == b"body"


def test_waits_between_consecutive_requests() -> None:
    transport, clock = make_transport([FakeResponse(200), FakeResponse(200)], wait_seconds=2.0)
    transport.get("https://x/a")
    transport.get("https://x/b")
    assert clock.sleeps == [2.0]


def test_retries_on_429_with_growing_wait() -> None:
    transport, clock = make_transport(
        [FakeResponse(429), FakeResponse(503), FakeResponse(200, b"al fin")],
        wait_seconds=0.0,
        retry_wait_seconds=5.0,
    )
    assert transport.get("https://x/a") == b"al fin"
    assert clock.sleeps == [5.0, 10.0]


def test_gives_up_after_max_retries() -> None:
    transport, _ = make_transport(
        [FakeResponse(429)] * 3, wait_seconds=0.0, max_retries=2, retry_wait_seconds=0.0
    )
    with pytest.raises(SourceHTTPError, match="HTTP 429"):
        transport.get("https://x/a")


def test_non_retryable_status_fails_immediately() -> None:
    transport, _ = make_transport([FakeResponse(404)], wait_seconds=0.0)
    with pytest.raises(SourceHTTPError, match="HTTP 404"):
        transport.get("https://x/a")


def test_retries_on_transport_timeout_then_succeeds() -> None:
    transport, clock = make_transport(
        [FakeTimeout(), FakeResponse(200, b"al fin")],
        wait_seconds=0.0,
        retry_wait_seconds=5.0,
        retryable_exceptions=(FakeTimeout,),
    )
    assert transport.get("https://x/a") == b"al fin"
    assert clock.sleeps == [5.0]


def test_persistent_timeout_becomes_504_not_a_crash() -> None:
    # Agotados los reintentos, un timeout se traduce a SourceHTTPError 504: la
    # fuente lo trata como fallo saltable, no revienta el run entero.
    transport, _ = make_transport(
        [FakeTimeout()] * 3,
        wait_seconds=0.0,
        max_retries=2,
        retry_wait_seconds=0.0,
        retryable_exceptions=(FakeTimeout,),
    )
    with pytest.raises(SourceHTTPError, match="HTTP 504"):
        transport.get("https://x/a")


# --- Desbordamiento a proxy (ScrapeOps, ADR 0004) ----------------------------


def _wrapped(url: str, params: str) -> FakeCall:
    """La llamada tal como la ve la sesión cuando va por ScrapeOps."""
    return FakeCall(SCRAPEOPS_ENDPOINT, {"api_key": "secret-key", "url": f"{url}?{params}"})


def test_without_proxy_requests_target_directly() -> None:
    clock = FakeClock()
    session = FakeSession([FakeResponse(200, b"body")])
    transport = HttpTransport(session=session, sleep=clock.sleep, clock=clock, wait_seconds=0.0)
    assert transport.get("https://sofascore/api", params={"q": "fores"}) == b"body"
    assert session.calls == [FakeCall("https://sofascore/api", {"q": "fores"})]


def test_starts_direct_even_when_overflow_available() -> None:
    # El proxy es desbordamiento: mientras no haya bloqueo, todo va directo
    # (gratis), aunque haya clave y la fuente lo permita.
    clock = FakeClock()
    session = FakeSession([FakeResponse(200, b"body")])
    transport = HttpTransport(
        session=session,
        overflow_proxy=ScrapeOpsProxy("secret-key"),
        sleep=clock.sleep,
        clock=clock,
        wait_seconds=0.0,
    )
    assert transport.get("https://sofascore/api", params={"q": "fores"}) == b"body"
    assert session.calls == [FakeCall("https://sofascore/api", {"q": "fores"})]


def test_switches_to_proxy_on_persistent_block() -> None:
    # Un 429 que persiste tras el primer reintento directo confirma el bloqueo:
    # se conmuta y se reintenta por proxy dentro del mismo run (no se aborta ni
    # se salta el jugador). El primer reintento gasta un solo backoff (5 s), no
    # los 5-10-20 s completos.
    clock = FakeClock()
    session = FakeSession([FakeResponse(429), FakeResponse(429), FakeResponse(200, b"body")])
    transport = HttpTransport(
        session=session,
        overflow_proxy=ScrapeOpsProxy("secret-key"),
        sleep=clock.sleep,
        clock=clock,
        wait_seconds=0.0,
        retry_wait_seconds=5.0,
    )
    assert transport.get("https://sofascore/api", params={"q": "fores"}) == b"body"
    assert session.calls == [
        FakeCall("https://sofascore/api", {"q": "fores"}),
        FakeCall("https://sofascore/api", {"q": "fores"}),
        _wrapped("https://sofascore/api", "q=fores"),
    ]
    assert clock.sleeps == [5.0]


def test_first_block_is_a_direct_retry_before_switching() -> None:
    # Un 429 aislado no dispara el proxy: se reintenta directo por si es un blip.
    clock = FakeClock()
    session = FakeSession([FakeResponse(429), FakeResponse(200, b"body")])
    transport = HttpTransport(
        session=session,
        overflow_proxy=ScrapeOpsProxy("secret-key"),
        sleep=clock.sleep,
        clock=clock,
        wait_seconds=0.0,
        retry_wait_seconds=5.0,
    )
    assert transport.get("https://sofascore/api") == b"body"
    assert session.calls == [
        FakeCall("https://sofascore/api", None),
        FakeCall("https://sofascore/api", None),
    ]


def test_stays_on_proxy_after_switching() -> None:
    # Tras conmutar, las peticiones siguientes de esa fuente van directas por
    # proxy: no se re-prueba la vía directa en el mismo run.
    clock = FakeClock()
    session = FakeSession(
        [FakeResponse(429), FakeResponse(429), FakeResponse(200, b"one"), FakeResponse(200, b"two")]
    )
    transport = HttpTransport(
        session=session,
        overflow_proxy=ScrapeOpsProxy("secret-key"),
        sleep=clock.sleep,
        clock=clock,
        wait_seconds=0.0,
    )
    assert transport.get("https://sofascore/a") == b"one"
    assert transport.get("https://sofascore/b") == b"two"
    # La segunda petición sale ya por proxy en su primer intento.
    assert session.calls[-1] == FakeCall(
        SCRAPEOPS_ENDPOINT, {"api_key": "secret-key", "url": "https://sofascore/b"}
    )


def test_403_also_triggers_overflow() -> None:
    # 403 (reto de Cloudflare / IP vetada) también gatilla el desbordamiento.
    clock = FakeClock()
    session = FakeSession([FakeResponse(403), FakeResponse(403), FakeResponse(200, b"body")])
    transport = HttpTransport(
        session=session,
        overflow_proxy=ScrapeOpsProxy("secret-key"),
        sleep=clock.sleep,
        clock=clock,
        wait_seconds=0.0,
    )
    assert transport.get("https://sofascore/api") == b"body"
    assert session.calls[-1] == FakeCall(
        SCRAPEOPS_ENDPOINT, {"api_key": "secret-key", "url": "https://sofascore/api"}
    )


def test_without_key_429_retries_directly_and_never_proxies() -> None:
    # Sin LFDATA_SCRAPEOPS_KEY (overflow_proxy None) el comportamiento es el de
    # siempre: reintentos directos con backoff creciente, sin conmutar nunca.
    transport, clock = make_transport(
        [FakeResponse(429), FakeResponse(429), FakeResponse(200, b"al fin")],
        wait_seconds=0.0,
        retry_wait_seconds=5.0,
    )
    assert transport.get("https://x/a") == b"al fin"
    assert clock.sleeps == [5.0, 10.0]


def test_403_without_proxy_suggests_enabling_it() -> None:
    transport, _ = make_transport([FakeResponse(403)], wait_seconds=0.0)
    with pytest.raises(SourceHTTPError, match="proxy=true"):
        transport.get("https://sofascore/api")


def test_403_after_switch_does_not_suggest_proxy() -> None:
    # Ya conmutados a proxy, un 403 no sugiere activar el proxy (ya está activo).
    clock = FakeClock()
    session = FakeSession([FakeResponse(429), FakeResponse(429), FakeResponse(403)])
    transport = HttpTransport(
        session=session,
        overflow_proxy=ScrapeOpsProxy("k"),
        sleep=clock.sleep,
        clock=clock,
        wait_seconds=0.0,
    )
    with pytest.raises(SourceHTTPError) as excinfo:
        transport.get("https://sofascore/api")
    assert "proxy=true" not in str(excinfo.value)


def test_proxy_from_env_disabled_when_source_not_marked() -> None:
    assert scrapeops_proxy_from_env(enabled=False, env={"LFDATA_SCRAPEOPS_KEY": "k"}) is None


def test_proxy_from_env_disabled_without_key() -> None:
    assert scrapeops_proxy_from_env(enabled=True, env={}) is None


def test_proxy_from_env_enabled_with_key_and_mark() -> None:
    proxy = scrapeops_proxy_from_env(enabled=True, env={"LFDATA_SCRAPEOPS_KEY": "k"})
    assert isinstance(proxy, ScrapeOpsProxy)
