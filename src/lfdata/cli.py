"""CLI de lfdata.

Los subcomandos (ingest, backfill...) se registran aquí a medida que existen.
"""

import argparse
import logging
import os

from lfdata import __version__
from lfdata.sources.transfermarkt import DEFAULT_SEASON

DEFAULT_DATA_URI = "file://./data"
DEFAULT_MAPPINGS_DIR = "mappings"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lfdata",
        description="Datos y proyecciones para fantasy de La Liga.",
    )
    parser.add_argument("--version", action="version", version=f"lfdata {__version__}")
    subparsers = parser.add_subparsers(dest="command", title="comandos")

    ingest = subparsers.add_parser("ingest", help="Descarga una fuente a raw/ y curated/")
    ingest_sources = ingest.add_subparsers(dest="source", title="fuentes", required=True)

    biwenger = ingest_sources.add_parser("biwenger", help="Plantilla de una competición")
    biwenger.add_argument(
        "--competition",
        required=True,
        choices=("la-liga", "segunda-division"),
        help="Competición a ingerir",
    )
    biwenger.add_argument(
        "--season",
        help="Temporada (p. ej. 2026). Si se indica, añade fantasy_points y biwenger_prices",
    )
    biwenger.add_argument(
        "--delta",
        action="store_true",
        help=(
            "Refresh por deltas tras jornada: en vez de recorrer la plantilla entera, "
            "refresca solo a quienes puntuaron en las jornadas nuevas (requiere --season)"
        ),
    )
    biwenger.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Salta a los jugadores que ya tienen su report en raw/ (reanudar un backfill "
            "de temporada pasada inmutable sin re-descargar; requiere --season)"
        ),
    )
    biwenger.add_argument(
        "--since-days",
        type=int,
        default=None,
        help=(
            "Salta jugadores descargados en los últimos N días (reanudar el refresh de la "
            "temporada actual; requiere --season)"
        ),
    )
    biwenger.add_argument(
        "--data",
        default=os.environ.get("LFDATA_DATA", DEFAULT_DATA_URI),
        help=f"URI base del almacenamiento (por defecto {DEFAULT_DATA_URI} o $LFDATA_DATA)",
    )
    biwenger.set_defaults(func=_cmd_ingest_biwenger)

    rounds = ingest_sources.add_parser(
        "biwenger-rounds",
        help="Puntos por jornada de todos los jugadores (histórico sin sesgo)",
    )
    rounds.add_argument(
        "--competition",
        required=True,
        choices=("la-liga", "segunda-division"),
        help="Competición a ingerir",
    )
    rounds.add_argument(
        "--season",
        required=True,
        help="Temporada de Biwenger (p. ej. 2025 = 2024/2025)",
    )
    rounds.add_argument(
        "--resume",
        action="store_true",
        help="Salta las jornadas ya curadas (reanudar un backfill sin re-descargar)",
    )
    rounds.add_argument(
        "--data",
        default=os.environ.get("LFDATA_DATA", DEFAULT_DATA_URI),
        help=f"URI base del almacenamiento (por defecto {DEFAULT_DATA_URI} o $LFDATA_DATA)",
    )
    rounds.set_defaults(func=_cmd_ingest_biwenger_rounds)

    transfermarkt = ingest_sources.add_parser(
        "transfermarkt", help="Plantillas, valores de mercado y traspasos por competición"
    )
    transfermarkt.add_argument(
        "--competition",
        required=True,
        choices=("la-liga", "segunda-division"),
        help="Competición a ingerir",
    )
    transfermarkt.add_argument(
        "--season",
        type=int,
        default=DEFAULT_SEASON,
        help=f"saison_id de Transfermarkt (año de inicio; por defecto {DEFAULT_SEASON})",
    )
    transfermarkt.add_argument(
        "--max-clubs",
        type=int,
        default=None,
        help="Limita el número de clubes recorridos (prueba parcial)",
    )
    transfermarkt.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="Salta jugadores ya descargados en los últimos N días (reanudar backfill)",
    )
    transfermarkt.add_argument(
        "--data",
        default=os.environ.get("LFDATA_DATA", DEFAULT_DATA_URI),
        help=f"URI base del almacenamiento (por defecto {DEFAULT_DATA_URI} o $LFDATA_DATA)",
    )
    transfermarkt.set_defaults(func=_cmd_ingest_transfermarkt)

    probe = subparsers.add_parser(
        "probe",
        help="Sondas de diagnóstico de las fuentes (no escriben datos curados)",
    )
    probe_targets = probe.add_subparsers(dest="target", title="sondas", required=True)

    quota = probe_targets.add_parser(
        "biwenger-quota",
        help="Caracteriza la ventana de cuota de Biwenger (429→200) sondeando cada hora",
    )
    quota.add_argument(
        "--competition",
        default="la-liga",
        choices=("la-liga", "segunda-division"),
        help="Competición cuyos jugadores se sondean (por defecto la-liga)",
    )
    quota.add_argument(
        "--season",
        default="2026",
        help="Temporada del detalle por jugador que se sondea (por defecto 2026)",
    )
    quota.add_argument(
        "--interval-hours",
        type=float,
        default=1.0,
        help="Horas entre sondeos (por defecto 1)",
    )
    quota.add_argument(
        "--max-hours",
        type=float,
        default=24.0,
        help="Límite de horas antes de rendirse sin ver un 200 (por defecto 24)",
    )
    quota.add_argument(
        "--confirm-requests",
        type=int,
        default=3,
        help=(
            "Peticiones seguidas que deben pasar tras el primer 200 para dar la ventana por "
            "repuesta (la cuota oscila: un 200 aislado puede ser un blip; por defecto 3)"
        ),
    )
    quota.add_argument(
        "--measure-capacity",
        action="store_true",
        help=(
            "Tras confirmar la recuperación, sigue pidiendo para contar cuántas peticiones "
            "admite la ventana hasta el siguiente corte (opcional; quema la ventana repuesta)"
        ),
    )
    quota.add_argument(
        "--out",
        default=None,
        help="Ruta del registro JSON (por defecto biwenger-quota-probe-<competición>-<ts>.json)",
    )
    quota.set_defaults(func=_cmd_probe_biwenger_quota)

    direct = probe_targets.add_parser(
        "direct-access",
        help=(
            "Tarea de humo: lanza peticiones directas a Biwenger y SofaScore y registra "
            "los códigos (valida que las IPs de datacenter de AWS no están vetadas)"
        ),
    )
    direct.add_argument(
        "--source",
        action="append",
        dest="sources",
        choices=("biwenger", "sofascore"),
        help="Fuente a sondear (repetible); por defecto ambas",
    )
    direct.add_argument(
        "--competition",
        default="la-liga",
        choices=("la-liga", "segunda-division"),
        help="Competición cuyos jugadores se sondean en Biwenger (por defecto la-liga)",
    )
    direct.add_argument(
        "--season",
        default="2026",
        help="Temporada del detalle por jugador de Biwenger (por defecto 2026)",
    )
    direct.add_argument(
        "--requests",
        type=int,
        default=25,
        help="Peticiones directas por fuente (por defecto 25)",
    )
    direct.add_argument(
        "--wait-seconds",
        type=float,
        default=2.0,
        help="Segundos entre peticiones (por defecto 2)",
    )
    direct.add_argument(
        "--out",
        default=None,
        help="Ruta del registro JSON (por defecto direct-access-smoke-<ts>.json)",
    )
    direct.set_defaults(func=_cmd_probe_direct_access)

    mapper = subparsers.add_parser(
        "map",
        help="Genera y aplica los mappings Biwenger↔Transfermarkt (IDs canónicos)",
    )
    mapper.add_argument(
        "--check",
        action="store_true",
        help="No escribe: falla si hay datos de Biwenger sin mapping (CI y pipeline)",
    )
    mapper.add_argument(
        "--data",
        default=os.environ.get("LFDATA_DATA", DEFAULT_DATA_URI),
        help=f"URI base del almacenamiento (por defecto {DEFAULT_DATA_URI} o $LFDATA_DATA)",
    )
    mapper.add_argument(
        "--mappings",
        default=DEFAULT_MAPPINGS_DIR,
        help=f"Directorio de los ficheros de mappings (por defecto {DEFAULT_MAPPINGS_DIR}/)",
    )
    mapper.set_defaults(func=_cmd_map)

    return parser


def _report_ingest(result, competition: str, data: str) -> int:
    """Imprime filas por tabla, métricas, anomalías y fallos; 1 si hubo alguno."""
    for table, count in result.rows.items():
        print(f"{table}: {count} filas ({competition}) -> {data}")
    for name, count in result.stats.items():
        print(f"{name}: {count}")
    for reason, count in result.anomalies.items():
        print(f"anomalía: {count} {reason}")
    if not result.failures:
        return 0
    print(f"\n{len(result.failures)} jugadores fallaron y se saltaron:")
    for failure in result.failures:
        print(f"  - {failure}")
    return 1


def _cmd_ingest_biwenger(args: argparse.Namespace) -> int:
    from lfdata.sources.biwenger import ingest_reports, ingest_reports_delta, ingest_squad
    from lfdata.storage import Storage

    if args.delta and not args.season:
        print("--delta requiere --season")
        return 2
    if args.delta and (args.resume or args.since_days is not None):
        print("--resume y --since-days no aplican a --delta (ya es idempotente por jornada)")
        return 2

    storage = Storage(args.data)
    result = ingest_squad(storage, args.competition)
    if args.season:
        if args.delta:
            result |= ingest_reports_delta(storage, args.competition, args.season)
        else:
            result |= ingest_reports(
                storage,
                args.competition,
                args.season,
                since_days=args.since_days,
                resume=args.resume,
            )
    return _report_ingest(result, args.competition, args.data)


def _cmd_ingest_biwenger_rounds(args: argparse.Namespace) -> int:
    from lfdata.sources.biwenger import ingest_rounds
    from lfdata.storage import Storage

    storage = Storage(args.data)
    result = ingest_rounds(storage, args.competition, args.season, resume=args.resume)
    return _report_ingest(result, args.competition, args.data)


def _cmd_ingest_transfermarkt(args: argparse.Namespace) -> int:
    from lfdata.sources.transfermarkt import ingest_squads
    from lfdata.storage import Storage

    storage = Storage(args.data)
    result = ingest_squads(
        storage,
        args.competition,
        season=args.season,
        max_clubs=args.max_clubs,
        since_days=args.since_days,
    )
    return _report_ingest(result, args.competition, args.data)


def _cmd_probe_biwenger_quota(args: argparse.Namespace) -> int:
    from pathlib import Path

    from lfdata.sources.biwenger import default_out_path, run_probe

    out_path = Path(args.out) if args.out else default_out_path(args.competition)
    report = run_probe(
        args.competition,
        out_path,
        interval_seconds=args.interval_hours * 3600.0,
        max_hours=args.max_hours,
        measure_capacity=args.measure_capacity,
        confirm_requests=args.confirm_requests,
        season=args.season,
    )
    print(report.summary())
    print(f"Registro en {out_path}")
    # "already-open" y "timed-out" no midieron la ventana: se señalan con código 1
    # para que un run desatendido lo detecte sin parsear el registro.
    return 0 if report.outcome == "recovered" else 1


def _cmd_probe_direct_access(args: argparse.Namespace) -> int:
    from pathlib import Path

    from lfdata.sources.smoke import SOURCES, default_out_path, run_smoke

    out_path = Path(args.out) if args.out else default_out_path()
    sources = tuple(args.sources) if args.sources else SOURCES
    reports = run_smoke(
        out_path,
        sources=sources,
        competition=args.competition,
        season=args.season,
        count=args.requests,
        wait_seconds=args.wait_seconds,
    )
    for report in reports.values():
        print(report.summary())
    print(f"Registro en {out_path}")
    # Un solo 403 (fuente vetada) da código 1 para que un run desatendido en Fargate
    # detecte el problema sin parsear el registro; "rate-limited" no veta la IP y no
    # bloquea la automatización, así que no cuenta como fallo.
    return 1 if any(r.verdict == "blocked" for r in reports.values()) else 0


def _cmd_map(args: argparse.Namespace) -> int:
    from lfdata.mappings import MappingIntegrityError, check_mappings, run_map
    from lfdata.storage import Storage

    storage = Storage(args.data)
    try:
        if args.check:
            problems = check_mappings(storage, args.mappings)
            for problem in problems:
                print(problem)
            if problems:
                print(
                    f"\n{len(problems)} filas sin mapping. "
                    "Ejecuta `lfdata map` y revisa los dudosos."
                )
                return 1
            print("Todos los jugadores y equipos de Biwenger tienen mapping.")
            return 0

        report = run_map(storage, args.mappings)
        print(report.render())
        print(f"Mappings escritos en {args.mappings}/")
        return 0
    except MappingIntegrityError as error:
        print("Integridad de mappings violada — corrige los ficheros y vuelve a ejecutar:")
        for problem in error.problems:
            print(f"  - {problem}")
        return 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
