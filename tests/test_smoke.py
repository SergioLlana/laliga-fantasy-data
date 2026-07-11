import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from lfdata.sources.smoke import (
    SourceAccessReport,
    default_out_path,
    probe_source_access,
    run_smoke,
)


class FakeClock:
    """Reloj de pared que solo avanza al dormir (como el de test_probe)."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


def responder(statuses):
    """``request`` que sirve la secuencia de estados dada como (url, status)."""
    pending = list(statuses)

    def request() -> tuple[str, int]:
        status = pending.pop(0)
        return f"https://example/{len(pending)}", status

    return request


def probe(statuses, **kwargs):
    clock = FakeClock()
    report = probe_source_access(
        responder(statuses),
        source="biwenger",
        count=len(statuses),
        now=clock,
        sleep=clock.sleep,
        wait_seconds=2.0,
        **kwargs,
    )
    return report, clock


def test_fires_count_requests_spacing_between_them() -> None:
    report, clock = probe([200, 200, 200])
    assert len(report.attempts) == 3
    # Espera entre peticiones, no antes de la primera ni tras la última.
    assert clock.sleeps == [2.0, 2.0]


def test_verdict_viable_when_any_200_and_no_403() -> None:
    report, _ = probe([200, 429, 200])
    assert report.verdict == "viable"
    assert report.status_counts == {200: 2, 429: 1}


def test_verdict_blocked_when_any_403() -> None:
    # Un solo 403 basta para veredicto de veto, aunque haya 200s.
    report, _ = probe([200, 200, 403])
    assert report.verdict == "blocked"


def test_verdict_rate_limited_when_only_429() -> None:
    report, _ = probe([429, 429, 429])
    assert report.verdict == "rate-limited"


def test_verdict_unreachable_when_only_network_errors() -> None:
    report, _ = probe([0, 0])
    assert report.verdict == "unreachable"


def test_on_attempt_fires_once_per_request_for_incremental_record() -> None:
    seen: list[int] = []
    clock = FakeClock()
    probe_source_access(
        responder([200, 429, 200]),
        source="sofascore",
        count=3,
        now=clock,
        sleep=clock.sleep,
        on_attempt=lambda report, attempt: seen.append(len(report.attempts)),
    )
    assert seen == [1, 2, 3]


def test_summary_mentions_verdict_and_counts() -> None:
    report, _ = probe([200, 403])
    text = report.summary()
    assert "VETADA" in text
    assert "1×200" in text and "1×403" in text


def test_report_serializes_verdict_and_status_counts() -> None:
    report = SourceAccessReport(
        source="biwenger", started_at=datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    )
    data = report.to_dict()
    assert data["source"] == "biwenger"
    assert data["verdict"] == "unreachable"
    assert data["status_counts"] == {}


class FakeSession:
    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.calls: list[str] = []

    def get(self, url, params=None):
        self.calls.append(url)
        return SimpleNamespace(status_code=self._statuses.pop(0), content=b"")


def test_run_smoke_writes_combined_record_for_each_source(tmp_path) -> None:
    out = tmp_path / "smoke.json"
    biwenger = FakeSession([200, 200, 429])
    sofascore = FakeSession([200, 403, 200])
    reports = run_smoke(
        out,
        count=3,
        slugs=["a", "b", "c"],
        biwenger_session=biwenger,
        sofascore_session=sofascore,
        sleep=lambda _: None,
    )
    assert reports["biwenger"].verdict == "viable"
    assert reports["sofascore"].verdict == "blocked"

    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["kind"] == "direct-access-smoke"
    assert set(saved["sources"]) == {"biwenger", "sofascore"}
    assert saved["sources"]["sofascore"]["verdict"] == "blocked"
    assert len(saved["sources"]["biwenger"]["attempts"]) == 3


def test_run_smoke_hits_player_detail_and_never_a_proxy(tmp_path) -> None:
    biwenger = FakeSession([200, 200, 200])
    sofascore = FakeSession([200, 200, 200])
    run_smoke(
        tmp_path / "s.json",
        count=3,
        slugs=["x", "y", "z"],
        biwenger_session=biwenger,
        sofascore_session=sofascore,
        sleep=lambda _: None,
    )
    # Biwenger: detalle por jugador (no la plantilla cacheada), rotando slugs.
    assert all("/players/la-liga/" in url for url in biwenger.calls)
    assert [url.rsplit("/", 1)[1] for url in biwenger.calls] == ["x", "y", "z"]
    # SofaScore: listado de temporadas por torneo, host directo de la API.
    assert all("api.sofascore.com" in url for url in sofascore.calls)
    assert all("/seasons" in url for url in sofascore.calls)
    # Nunca se enruta por el proxy de ScrapeOps.
    assert not any("scrapeops" in url for url in biwenger.calls + sofascore.calls)


def test_run_smoke_can_target_a_single_source(tmp_path) -> None:
    sofascore = FakeSession([200, 200])
    reports = run_smoke(
        tmp_path / "s.json",
        sources=("sofascore",),
        count=2,
        sofascore_session=sofascore,
        sleep=lambda _: None,
    )
    assert set(reports) == {"sofascore"}


def test_run_smoke_rejects_unknown_source(tmp_path) -> None:
    with pytest.raises(ValueError, match="Fuentes desconocidas"):
        run_smoke(tmp_path / "s.json", sources=("premier",))


def test_run_smoke_records_note_when_roster_unavailable(tmp_path) -> None:
    from lfdata.sources.biwenger.probe import RosterUnavailableError

    class NoRoster(FakeSession):
        def get(self, url, params=None):
            raise RosterUnavailableError("no hay plantilla")

    out = tmp_path / "s.json"
    reports = run_smoke(
        out,
        sources=("biwenger",),
        count=3,
        biwenger_session=NoRoster([]),
        sleep=lambda _: None,
    )
    report = reports["biwenger"]
    assert report.attempts == []
    assert report.verdict == "unreachable"
    assert "plantilla" in report.note


def test_default_out_path_is_timestamped() -> None:
    started = datetime(2026, 7, 11, 9, 5, 0, tzinfo=UTC)
    path = default_out_path(started=started)
    assert path.name == "direct-access-smoke-20260711T090500Z.json"
