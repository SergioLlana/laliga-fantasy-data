from dataclasses import dataclass, field

import pytest

from lfdata.sources.http import HttpTransport, SourceHTTPError


@dataclass
class FakeResponse:
    status_code: int
    content: bytes = b"ok"


@dataclass
class FakeSession:
    responses: list[FakeResponse]
    calls: list[str] = field(default_factory=list)

    def get(self, url, params=None):
        self.calls.append(url)
        return self.responses.pop(0)


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
