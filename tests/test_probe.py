import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from lfdata.sources.biwenger.probe import (
    ProbeReport,
    default_out_path,
    probe_quota_window,
    run_probe,
)


class FakeClock:
    """Reloj de pared que solo avanza al dormir (como el de test_http)."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


def responder(statuses):
    """Devuelve un ``request`` que sirve la secuencia dada; un Exception se lanza."""
    pending = list(statuses)

    def request() -> int:
        item = pending.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return request


def probe(statuses, **kwargs):
    clock = FakeClock()
    report = probe_quota_window(
        responder(statuses), now=clock, sleep=clock.sleep, interval_seconds=3600.0, **kwargs
    )
    return report, clock


def test_recovered_window_is_bracketed_by_first_429_and_200() -> None:
    report, clock = probe([429, 429, 200])
    assert report.outcome == "recovered"
    # primer 429 en T0, último 429 una hora después, 200 a las dos horas.
    assert report.window_seconds == 7200.0
    assert report.recovery_lower_seconds == 3600.0
    assert clock.sleeps == [3600.0, 3600.0]


def test_already_open_when_first_probe_is_200() -> None:
    report, _ = probe([200])
    assert report.outcome == "already-open"
    assert report.window_seconds is None
    assert report.recovered_at is None


def test_times_out_after_max_hours_without_a_200() -> None:
    report, clock = probe([429] * 10, max_hours=3.0)
    assert report.outcome == "timed-out"
    # Sondea en T0, +1h, +2h, +3h y se rinde antes de pasar del límite.
    assert len(report.attempts) == 4
    assert report.first_block_at is not None
    assert report.window_seconds is None


def test_network_error_does_not_abort_and_keeps_polling() -> None:
    report, _ = probe([RuntimeError("timeout"), 429, 200])
    assert report.outcome == "recovered"
    # El fallo de red cuenta como intento (estado sintético 0), no como bloqueo.
    assert [a.status for a in report.attempts] == [0, 429, 200]
    assert report.first_block_at == report.attempts[1].at


def test_on_attempt_fires_once_per_probe_for_incremental_record() -> None:
    seen: list[int] = []
    clock = FakeClock()
    probe_quota_window(
        responder([429, 429, 200]),
        now=clock,
        sleep=clock.sleep,
        interval_seconds=3600.0,
        on_attempt=lambda report, attempt: seen.append(len(report.attempts)),
    )
    assert seen == [1, 2, 3]


def test_measure_capacity_counts_requests_until_next_block() -> None:
    # Tras el 200 que abre la ventana (cuenta 1), admite dos 200 más y corta.
    report, _ = probe(
        [429, 200, 200, 200, 429],
        measure_capacity=True,
        capacity_wait_seconds=2.0,
        max_capacity_requests=300,
    )
    assert report.outcome == "recovered"
    assert report.capacity_requests == 3


def test_capacity_is_skipped_when_flag_off() -> None:
    report, _ = probe([429, 200, 200, 429])
    assert report.capacity_requests is None
    # Sin medir capacidad, la sonda para en el primer 200: no gasta los siguientes.
    assert len(report.attempts) == 2


class FakeSession:
    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None):
        self.calls.append((url, dict(params) if params else None))
        return SimpleNamespace(status_code=self._statuses.pop(0))


def test_run_probe_writes_record_and_never_uses_a_proxy(tmp_path) -> None:
    out = tmp_path / "probe.json"
    session = FakeSession([429, 200])
    report = run_probe(
        "la-liga", out, interval_seconds=0.0, max_hours=1.0, session=session
    )
    assert report.outcome == "recovered"
    # Va directo al host de Biwenger, nunca al endpoint de ScrapeOps.
    assert all("biwenger.com" in url for url, _ in session.calls)
    assert not any("scrapeops" in url for url, _ in session.calls)

    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["source"] == "biwenger"
    assert saved["competition"] == "la-liga"
    assert saved["outcome"] == "recovered"
    assert len(saved["attempts"]) == 2


def test_run_probe_rejects_unknown_competition(tmp_path) -> None:
    with pytest.raises(ValueError, match="Competición desconocida"):
        run_probe("premier", tmp_path / "x.json", session=FakeSession([200]))


def test_default_out_path_is_timestamped_per_competition() -> None:
    started = datetime(2026, 7, 10, 9, 5, 0, tzinfo=UTC)
    path = default_out_path("segunda-division", started=started)
    assert path.name == "biwenger-quota-probe-segunda-division-20260710T090500Z.json"


def test_summary_is_human_readable_for_each_outcome() -> None:
    recovered, _ = probe([429, 429, 200])
    assert "ventana repuesta" in recovered.summary()
    already, _ = probe([200])
    assert "ya estaba abierta" in already.summary()
    timed, _ = probe([429] * 10, max_hours=2.0)
    assert "sin 200" in timed.summary()


def test_report_serializes_datetimes_as_iso() -> None:
    report = ProbeReport(started_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
    assert report.to_dict()["started_at"] == "2026-07-10T12:00:00+00:00"
