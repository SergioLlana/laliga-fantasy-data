"""CLI de lfdata.

Los subcomandos (ingest, backfill...) se registran aquí a medida que existen.
"""

import argparse

from lfdata import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lfdata",
        description="Datos y proyecciones para fantasy de La Liga.",
    )
    parser.add_argument("--version", action="version", version=f"lfdata {__version__}")
    parser.add_subparsers(dest="command", title="comandos")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
