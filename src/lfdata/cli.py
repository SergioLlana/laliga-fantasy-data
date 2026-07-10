"""CLI de lfdata.

Los subcomandos (ingest, backfill...) se registran aquí a medida que existen.
"""

import argparse
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


def _cmd_ingest_biwenger(args: argparse.Namespace) -> int:
    from lfdata.sources.biwenger import ingest_reports, ingest_squad
    from lfdata.storage import Storage

    storage = Storage(args.data)
    rows = ingest_squad(storage, args.competition)
    if args.season:
        rows |= ingest_reports(storage, args.competition, args.season)
    for table, count in rows.items():
        print(f"{table}: {count} filas ({args.competition}) -> {args.data}")
    return 0


def _cmd_ingest_transfermarkt(args: argparse.Namespace) -> int:
    from lfdata.sources.transfermarkt import ingest_squads
    from lfdata.storage import Storage

    storage = Storage(args.data)
    rows = ingest_squads(
        storage,
        args.competition,
        season=args.season,
        max_clubs=args.max_clubs,
        since_days=args.since_days,
    )
    for table, count in rows.items():
        print(f"{table}: {count} filas ({args.competition}) -> {args.data}")
    return 0


def _cmd_map(args: argparse.Namespace) -> int:
    from lfdata.mappings import check_mappings, run_map
    from lfdata.storage import Storage

    storage = Storage(args.data)
    if args.check:
        problems = check_mappings(storage, args.mappings)
        for problem in problems:
            print(problem)
        if problems:
            print(
                f"\n{len(problems)} filas sin mapping. Ejecuta `lfdata map` y revisa los dudosos."
            )
            return 1
        print("Todos los jugadores y equipos de Biwenger tienen mapping.")
        return 0

    report = run_map(storage, args.mappings)
    print(report.render())
    print(f"Mappings escritos en {args.mappings}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
