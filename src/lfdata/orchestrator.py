"""Orquestador reanudable del runbook (issue #99): ``lfdata run backfill``/``incremental``.

Encadena los pasos de :doc:`../docs/runbook.md` en el orden correcto. Cada
sub-paso **ya es idempotente y reanudable** por separado (``--resume``,
``--since-days``, skip de partidos ya en ``raw/``): el orquestador no lleva
checkpoint propio, solo se detiene limpio en el primer paso con fallos y deja un
resumen claro de qué se completó y qué quedó pendiente. Relanzar el mismo comando
retoma sin re-descargar lo ya bajado ni duplicar en curated, apoyado en esa
reanudabilidad de cada sub-paso — no hace falta guardar en qué paso se quedó.

Un paso con fallos (429/404 saltados, un partido que no bajó) para el pipeline en
vez de seguir a los pasos siguientes, que suelen depender de que el anterior haya
terminado (``map`` necesita las plantillas ya curadas, issue #98). El re-lanzado
—manual o por cron/Fargate (#24)— es la vía de reintento.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from lfdata.mappings import MappingIntegrityError, MapReport, check_mappings, run_map
from lfdata.newcomers import ingest_newcomers
from lfdata.sources.biwenger import (
    ingest_reports,
    ingest_reports_delta,
    ingest_rounds,
    ingest_squad,
)
from lfdata.sources.ingestion import IngestResult
from lfdata.sources.sofascore import (
    TOURNAMENTS,
    backfill_cups_for_year,
    backfill_league_season_for_year,
    build_catalog,
    rebuild_cups,
    restamp_canonical,
)
from lfdata.sources.transfermarkt import ingest_squad_values, ingest_squads
from lfdata.storage import Storage

# Copa del Rey y las tres competiciones UEFA (runbook, paso 9 del backfill y
# bloque "tras cada jornada" del incremental): mismo cuarteto en ambos ciclos.
CUP_COMPETITIONS = ("copa-del-rey", "champions-league", "europa-league", "conference-league")


@dataclass
class StepResult:
    """Resultado uniforme de un paso, venga de un ``IngestResult``, un ``MapReport``..."""

    lines: list[str] = field(default_factory=list)
    ok: bool = True


@dataclass
class Step:
    name: str
    run: Callable[[], StepResult]


@dataclass
class StepOutcome:
    name: str
    result: StepResult


@dataclass
class RunReport:
    """Resumen de una ejecución del orquestador, paso a paso."""

    run_id: str
    outcomes: list[StepOutcome] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Sin pasos pendientes y ninguno con fallos: la corrida terminó limpia."""
        return not self.pending and all(outcome.result.ok for outcome in self.outcomes)

    def render(self) -> str:
        lines = [f"=== {self.run_id} ==="]
        for outcome in self.outcomes:
            status = "OK" if outcome.result.ok else "FALLOS"
            lines.append(f"\n--- {outcome.name} [{status}] ---")
            lines += outcome.result.lines
        if self.pending:
            lines.append("\nPasos pendientes (no llegaron a ejecutarse; relanza para retomar):")
            lines += [f"  - {name}" for name in self.pending]
        return "\n".join(lines)


def _ingest(result: IngestResult, label: str) -> StepResult:
    return StepResult(lines=result.render(label).splitlines(), ok=not result.failures)


def _map(report: MapReport) -> StepResult:
    # El mapping nunca detiene el pipeline por sí solo: lo dudoso va a revisión
    # (mappings/*-review.csv), no es un fallo de la ingesta.
    return StepResult(lines=report.render().splitlines())


def _map_check(storage: Storage, mappings_dir: str) -> StepResult:
    problems = check_mappings(storage, mappings_dir)
    if not problems:
        return StepResult(lines=["Todos los jugadores y equipos de Biwenger tienen mapping."])
    lines = [f"{len(problems)} filas sin mapping:"] + [f"  - {p}" for p in problems]
    return StepResult(lines=lines, ok=False)


def run_steps(run_id: str, steps: list[Step]) -> RunReport:
    """Ejecuta ``steps`` en orden; para en el primer fallo y reporta el resto como pendiente."""
    report = RunReport(run_id=run_id)
    remaining = [step.name for step in steps]
    for step in steps:
        remaining.pop(0)
        try:
            result = step.run()
        except MappingIntegrityError as error:
            result = StepResult(
                lines=["Integridad de mappings violada — corrige los ficheros y relanza:"]
                + [f"  - {p}" for p in error.problems],
                ok=False,
            )
        report.outcomes.append(StepOutcome(step.name, result))
        if not result.ok:
            report.pending = remaining
            break
    return report


def _cup_steps(storage: Storage, season: int, mappings_dir: str, prefix: str) -> list[Step]:
    steps = []
    for competition in CUP_COMPETITIONS:

        def run(competition: str = competition) -> StepResult:
            result = backfill_cups_for_year(storage, competition, season)
            result |= rebuild_cups(storage, mappings_dir=mappings_dir)
            return _ingest(result, f"{competition} {season}")

        steps.append(Step(f"{prefix}-{competition}", run))
    return steps


def backfill_steps(storage: Storage, season: int, *, mappings_dir: str = "mappings") -> list[Step]:
    """Los diez pasos del backfill de una temporada de La Liga (runbook, sección Backfill)."""
    competition = "la-liga"
    return [
        Step(
            "ingest-biwenger",
            lambda: _ingest(
                ingest_squad(storage, competition)
                | ingest_reports(storage, competition, str(season), resume=True),
                competition,
            ),
        ),
        Step(
            "ingest-biwenger-rounds",
            lambda: _ingest(
                ingest_rounds(storage, competition, str(season), resume=True), competition
            ),
        ),
        Step(
            "ingest-transfermarkt",
            lambda: _ingest(
                ingest_squads(storage, competition, season=season, since_days=30), competition
            ),
        ),
        Step("map-transfermarkt", lambda: _map(run_map(storage, mappings_dir, season=season))),
        Step(
            "backfill-sofascore",
            lambda: _ingest(
                backfill_league_season_for_year(
                    storage, TOURNAMENTS[competition], season, mappings_dir=mappings_dir
                ),
                f"{competition} {season}",
            ),
        ),
        Step(
            "curate-sofascore-catalog",
            lambda: _ingest(build_catalog(storage), "sofascore-catalog"),
        ),
        Step("map-sofascore", lambda: _map(run_map(storage, mappings_dir, season=season))),
        Step(
            "curate-sofascore-canonical",
            lambda: _ingest(
                restamp_canonical(storage, mappings_dir=mappings_dir), "sofascore-canonical"
            ),
        ),
        *_cup_steps(storage, season, mappings_dir, "backfill-sofascore-cups"),
        Step(
            "ingest-transfermarkt-values",
            lambda: _ingest(
                ingest_squad_values(storage, season=season, mappings_dir=mappings_dir),
                f"squad_values {season}",
            ),
        ),
    ]


def jornada_steps(
    storage: Storage, season: int, *, mappings_dir: str = "mappings", competition: str = "la-liga"
) -> list[Step]:
    """Ciclo "tras cada jornada" del incremental (runbook, sección Incremental)."""
    return [
        Step(
            "ingest-biwenger-delta",
            lambda: _ingest(
                ingest_squad(storage, competition)
                | ingest_reports_delta(storage, competition, str(season)),
                competition,
            ),
        ),
        Step(
            "ingest-biwenger-rounds",
            lambda: _ingest(
                ingest_rounds(storage, competition, str(season), resume=True), competition
            ),
        ),
        Step(
            "backfill-sofascore",
            lambda: _ingest(
                backfill_league_season_for_year(
                    storage, TOURNAMENTS[competition], season, mappings_dir=mappings_dir
                ),
                f"{competition} {season}",
            ),
        ),
        Step(
            "curate-sofascore-catalog",
            lambda: _ingest(build_catalog(storage), "sofascore-catalog"),
        ),
        *_cup_steps(storage, season, mappings_dir, "sofascore-cups"),
    ]


def semanal_steps(
    storage: Storage, season: int, *, mappings_dir: str = "mappings", competition: str = "la-liga"
) -> list[Step]:
    """Ciclo "semanal" del incremental (runbook, sección Incremental)."""
    return [
        Step(
            "ingest-transfermarkt",
            lambda: _ingest(
                ingest_squads(storage, competition, season=season, since_days=7), competition
            ),
        ),
        Step(
            "curate-sofascore-catalog",
            lambda: _ingest(build_catalog(storage), "sofascore-catalog"),
        ),
        Step(
            "newcomers",
            lambda: _ingest(
                ingest_newcomers(storage, competition, season, mappings_dir=mappings_dir),
                f"{competition} {season}",
            ),
        ),
        Step(
            "curate-sofascore-canonical",
            lambda: _ingest(
                restamp_canonical(storage, mappings_dir=mappings_dir), "sofascore-canonical"
            ),
        ),
        Step(
            "ingest-transfermarkt-values",
            lambda: _ingest(
                ingest_squad_values(storage, season=season, mappings_dir=mappings_dir),
                f"squad_values {season}",
            ),
        ),
        Step("map-check", lambda: _map_check(storage, mappings_dir)),
    ]
