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
    """Imprime filas por tabla, anomalías y el resumen de fallos; 1 si hubo alguno."""
    for table, count in result.rows.items():
        print(f"{table}: {count} filas ({competition}) -> {data}")
    for reason, count in result.anomalies.items():
        print(f"anomalía: {count} {reason}")
    if not result.failures:
        return 0
    print(f"\n{len(result.failures)} jugadores fallaron y se saltaron:")
    for failure in result.failures:
        print(f"  - {failure}")
    return 1


def _cmd_ingest_biwenger(args: argparse.Namespace) -> int:
    from lfdata.sources.biwenger import ingest_reports, ingest_squad
    from lfdata.storage import Storage

    storage = Storage(args.data)
    result = ingest_squad(storage, args.competition)
    if args.season:
        result |= ingest_reports(storage, args.competition, args.season)
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
