"""Tests del orquestador (issue #99): mecánica de ``run_steps`` y orden de los pipelines.

Sin red: la mecánica de parada-en-el-primer-fallo se prueba con pasos sintéticos,
y el orden de los pipelines llamando a los *builders* sin ejecutar ningún paso
(``Step.run`` es perezoso: construir la lista no dispara ninguna petición).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lfdata.cli import main
from lfdata.mappings.store import MappingIntegrityError
from lfdata.orchestrator import (
    Step,
    StepResult,
    backfill_steps,
    jornada_steps,
    run_steps,
    semanal_steps,
)
from lfdata.sources.ingestion import IngestResult, PlayerFailure
from lfdata.storage import Storage

# --- mecánica de run_steps ----------------------------------------------------


def test_run_steps_all_ok() -> None:
    calls: list[str] = []
    steps = [
        Step("uno", lambda: calls.append("uno") or StepResult(lines=["ok"])),
        Step("dos", lambda: calls.append("dos") or StepResult(lines=["ok"])),
    ]

    report = run_steps("run-1", steps)

    assert calls == ["uno", "dos"]
    assert report.ok
    assert report.pending == []
    assert [o.name for o in report.outcomes] == ["uno", "dos"]


def test_run_steps_stops_at_first_failure() -> None:
    calls: list[str] = []
    steps = [
        Step("uno", lambda: calls.append("uno") or StepResult(lines=["ok"])),
        Step("dos", lambda: calls.append("dos") or StepResult(lines=["falló"], ok=False)),
        Step("tres", lambda: calls.append("tres") or StepResult(lines=["ok"])),
    ]

    report = run_steps("run-1", steps)

    # "tres" nunca se ejecuta: un paso con fallos no arrastra al siguiente.
    assert calls == ["uno", "dos"]
    assert not report.ok
    assert report.pending == ["tres"]
    assert [o.name for o in report.outcomes] == ["uno", "dos"]


def test_run_steps_catches_mapping_integrity_error() -> None:
    def boom() -> StepResult:
        raise MappingIntegrityError(["fila rota"])

    steps = [Step("map", boom), Step("siguiente", lambda: StepResult(lines=["ok"]))]

    report = run_steps("run-1", steps)

    assert not report.ok
    assert report.pending == ["siguiente"]
    assert not report.outcomes[0].result.ok
    assert "fila rota" in "\n".join(report.outcomes[0].result.lines)


def test_report_render_lists_pending_steps() -> None:
    steps = [
        Step("uno", lambda: StepResult(lines=["1 filas"])),
        Step("dos", lambda: StepResult(lines=["fallo"], ok=False)),
        Step("tres", lambda: StepResult(lines=["ok"])),
    ]

    rendered = run_steps("mi-run", steps).render()

    assert "mi-run" in rendered
    assert "--- uno [OK] ---" in rendered
    assert "--- dos [FALLOS] ---" in rendered
    assert "tres" not in rendered.split("Pasos pendientes")[0]
    assert "Pasos pendientes" in rendered
    assert "  - tres" in rendered


# --- orden de los pipelines (sin ejecutar ningún paso) -------------------------


def test_backfill_steps_order() -> None:
    storage = Storage("file://./unused")
    names = [step.name for step in backfill_steps(storage, 2021)]

    assert names == [
        "ingest-biwenger",
        "ingest-biwenger-rounds",
        "ingest-transfermarkt",
        "map-transfermarkt",
        "backfill-sofascore",
        "curate-sofascore-catalog",
        "map-sofascore",
        "curate-sofascore-canonical",
        "backfill-sofascore-cups-copa-del-rey",
        "backfill-sofascore-cups-champions-league",
        "backfill-sofascore-cups-europa-league",
        "backfill-sofascore-cups-conference-league",
        "ingest-transfermarkt-values",
    ]


def test_jornada_steps_order() -> None:
    storage = Storage("file://./unused")
    names = [step.name for step in jornada_steps(storage, 2026)]

    assert names == [
        "ingest-biwenger-delta",
        "ingest-biwenger-rounds",
        "backfill-sofascore",
        "curate-sofascore-catalog",
        "sofascore-cups-copa-del-rey",
        "sofascore-cups-champions-league",
        "sofascore-cups-europa-league",
        "sofascore-cups-conference-league",
    ]


def test_semanal_steps_order() -> None:
    storage = Storage("file://./unused")
    names = [step.name for step in semanal_steps(storage, 2026)]

    assert names == [
        "ingest-transfermarkt",
        "curate-sofascore-catalog",
        "newcomers",
        "curate-sofascore-canonical",
        "ingest-transfermarkt-values",
        "map-check",
    ]


# --- CLI: encadena los pasos reales y para limpio en el primer fallo ----------


def test_cli_run_backfill_stops_at_first_failing_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def fake_ingest_squad(storage, competition):
        calls.append("ingest_squad")
        return IngestResult(rows={"biwenger_players": 1})

    def fake_ingest_reports(storage, competition, season, **kwargs):
        calls.append("ingest_reports")
        return IngestResult(
            rows={"fantasy_points": 0},
            failures=[PlayerFailure("Fulano", "https://example/players/1", 429)],
        )

    def fake_ingest_rounds(*args, **kwargs):
        calls.append("ingest_rounds")
        return IngestResult()

    monkeypatch.setattr("lfdata.orchestrator.ingest_squad", fake_ingest_squad)
    monkeypatch.setattr("lfdata.orchestrator.ingest_reports", fake_ingest_reports)
    monkeypatch.setattr("lfdata.orchestrator.ingest_rounds", fake_ingest_rounds)

    exit_code = main(["run", "backfill", "--season", "2021", "--data", f"file://{tmp_path}"])

    assert exit_code == 1
    # El paso 1 falló (429): el paso 2, que dependería de él, ni se ejecuta.
    assert calls == ["ingest_squad", "ingest_reports"]
    out = capsys.readouterr().out
    assert "ingest-biwenger [FALLOS]" in out
    assert "Pasos pendientes" in out
    assert "ingest-biwenger-rounds" in out


def test_cli_run_incremental_semanal_all_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lfdata.newcomers import TABLE as NEWCOMERS_TABLE

    monkeypatch.setattr(
        "lfdata.orchestrator.ingest_squads",
        lambda *a, **k: IngestResult(rows={"transfermarkt_players": 1}),
    )
    monkeypatch.setattr(
        "lfdata.orchestrator.build_catalog",
        lambda *a, **k: IngestResult(rows={"sofascore_players": 1}),
    )
    monkeypatch.setattr(
        "lfdata.orchestrator.ingest_newcomers",
        lambda *a, **k: IngestResult(rows={NEWCOMERS_TABLE: 0}),
    )
    monkeypatch.setattr(
        "lfdata.orchestrator.restamp_canonical",
        lambda *a, **k: IngestResult(rows={"player_match_stats": 1}),
    )
    monkeypatch.setattr(
        "lfdata.orchestrator.ingest_squad_values",
        lambda *a, **k: IngestResult(rows={"squad_values": 1}),
    )

    exit_code = main(
        [
            "run",
            "incremental",
            "--season",
            "2026",
            "--cycle",
            "semanal",
            "--data",
            f"file://{tmp_path}",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "map-check [OK] ---" in out
    assert "Todos los jugadores y equipos de Biwenger tienen mapping." in out
    assert "Pasos pendientes" not in out
