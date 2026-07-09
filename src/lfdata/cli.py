"""CLI de lfdata.

Los subcomandos (ingest, backfill...) se registran aquí a medida que existen.
"""

import argparse
import os

from lfdata import __version__
from lfdata.sources.transfermarkt import DEFAULT_SEASON

DEFAULT_DATA_URI = "file://./data"


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
        "--data",
        default=os.environ.get("LFDATA_DATA", DEFAULT_DATA_URI),
        help=f"URI base del almacenamiento (por defecto {DEFAULT_DATA_URI} o $LFDATA_DATA)",
    )
    transfermarkt.set_defaults(func=_cmd_ingest_transfermarkt)

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
    )
    for table, count in rows.items():
        print(f"{table}: {count} filas ({args.competition}) -> {args.data}")
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
