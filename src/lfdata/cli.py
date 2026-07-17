"""CLI de lfdata.

Los subcomandos (ingest, backfill...) se registran aquí a medida que existen.
"""

import argparse
import logging
import os

from lfdata import __version__
from lfdata.newcomers import SINCE_DAYS as NEWCOMER_SINCE_DAYS
from lfdata.sources.sofascore import TOURNAMENTS as SOFASCORE_TOURNAMENTS
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
        choices=("la-liga",),
        help="Competición a ingerir (de Biwenger solo se ingiere la-liga, ADR 0008)",
    )
    biwenger.add_argument(
        "--season",
        help=(
            "Año de inicio de la temporada (2025 = 2025/26), como en las demás fuentes. "
            "Si se indica, añade fantasy_points y biwenger_prices"
        ),
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
        choices=("la-liga",),
        help="Competición a ingerir (de Biwenger solo se ingiere la-liga, ADR 0008)",
    )
    rounds.add_argument(
        "--season",
        required=True,
        help="Año de inicio de la temporada (2025 = 2025/26), como en las demás fuentes",
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

    sofascore = ingest_sources.add_parser(
        "sofascore",
        help="Historial completo de un jugador (bajo demanda, cualquier liga)",
    )
    sofascore.add_argument(
        "--player",
        required=True,
        help="Jugador a descargar: canonical_id (p00001), id de SofaScore o nombre",
    )
    sofascore.add_argument(
        "--data",
        default=os.environ.get("LFDATA_DATA", DEFAULT_DATA_URI),
        help=f"URI base del almacenamiento (por defecto {DEFAULT_DATA_URI} o $LFDATA_DATA)",
    )
    sofascore.add_argument(
        "--mappings",
        default=DEFAULT_MAPPINGS_DIR,
        help=f"Directorio de los ficheros de mappings (por defecto {DEFAULT_MAPPINGS_DIR}/)",
    )
    sofascore.set_defaults(func=_cmd_ingest_sofascore)

    backfill = subparsers.add_parser(
        "backfill", help="Backfill masivo por liga-temporada (histórico)"
    )
    backfill_sources = backfill.add_subparsers(dest="source", title="fuentes", required=True)

    bf_sofascore = backfill_sources.add_parser(
        "sofascore",
        help="Eventing por jugador-partido de una liga-temporada (calendario→alineaciones)",
    )
    bf_sofascore.add_argument(
        "--competition",
        required=True,
        choices=tuple(SOFASCORE_TOURNAMENTS),
        help="Competición a backfillear",
    )
    bf_sofascore.add_argument(
        "--season",
        type=int,
        required=True,
        help="Año de inicio de la temporada (2025 = 2025/26), como en las demás fuentes",
    )
    bf_sofascore.add_argument(
        "--max-matches",
        type=int,
        default=None,
        help="Limita el número de partidos nuevos descargados (prueba parcial)",
    )
    bf_sofascore.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limita las páginas del calendario recorridas (prueba parcial)",
    )
    bf_sofascore.add_argument(
        "--data",
        default=os.environ.get("LFDATA_DATA", DEFAULT_DATA_URI),
        help=f"URI base del almacenamiento (por defecto {DEFAULT_DATA_URI} o $LFDATA_DATA)",
    )
    bf_sofascore.add_argument(
        "--mappings",
        default=DEFAULT_MAPPINGS_DIR,
        help=f"Directorio de los ficheros de mappings (por defecto {DEFAULT_MAPPINGS_DIR}/)",
    )
    bf_sofascore.set_defaults(func=_cmd_backfill_sofascore)

    curate = subparsers.add_parser(
        "curate",
        help="Reconstruye tablas curadas desde raw/ o desde otras curadas (sin peticiones)",
    )
    curate_targets = curate.add_subparsers(dest="target", title="tablas", required=True)

    so_catalog = curate_targets.add_parser(
        "sofascore-catalog",
        help=(
            "Publica sofascore_players y sofascore_teams desde raw/ (event-lineups + "
            "tournament-events): la evidencia de identidad del matcher, sin peticiones"
        ),
    )
    so_catalog.add_argument(
        "--data",
        default=os.environ.get("LFDATA_DATA", DEFAULT_DATA_URI),
        help=f"URI base del almacenamiento (por defecto {DEFAULT_DATA_URI} o $LFDATA_DATA)",
    )
    so_catalog.set_defaults(func=_cmd_curate_sofascore_catalog)

    so_canonical = curate_targets.add_parser(
        "sofascore-canonical",
        help=(
            "Rellena canonical_id en player_match_stats y player_season_stats cruzando "
            "con los mappings y reescribiendo la partición (sin releer raw/)"
        ),
    )
    so_canonical.add_argument(
        "--data",
        default=os.environ.get("LFDATA_DATA", DEFAULT_DATA_URI),
        help=f"URI base del almacenamiento (por defecto {DEFAULT_DATA_URI} o $LFDATA_DATA)",
    )
    so_canonical.add_argument(
        "--mappings",
        default=DEFAULT_MAPPINGS_DIR,
        help=f"Directorio de los ficheros de mappings (por defecto {DEFAULT_MAPPINGS_DIR}/)",
    )
    so_canonical.set_defaults(func=_cmd_curate_sofascore_canonical)

    so_matches = curate_targets.add_parser(
        "sofascore-matches",
        help=(
            "Re-cura player_match_stats desde raw/ (event-lineups + tournament-events): "
            "rehace la fila entera con la lógica y los mappings vigentes, sin peticiones"
        ),
    )
    so_matches.add_argument(
        "--data",
        default=os.environ.get("LFDATA_DATA", DEFAULT_DATA_URI),
        help=f"URI base del almacenamiento (por defecto {DEFAULT_DATA_URI} o $LFDATA_DATA)",
    )
    so_matches.add_argument(
        "--mappings",
        default=DEFAULT_MAPPINGS_DIR,
        help=f"Directorio de los ficheros de mappings (por defecto {DEFAULT_MAPPINGS_DIR}/)",
    )
    so_matches.set_defaults(func=_cmd_curate_sofascore_matches)

    crosscheck = subparsers.add_parser(
        "crosscheck",
        help="Informes de validación cruzada entre fuentes (no escriben datos curados)",
    )
    crosscheck_targets = crosscheck.add_subparsers(dest="target", title="cruces", required=True)

    minutes = crosscheck_targets.add_parser(
        "sofascore-biwenger-minutes",
        help="Compara los minutos de SofaScore con los de Biwenger (tolerancia 10 pp, umbral 95)",
    )
    minutes.add_argument(
        "--data",
        default=os.environ.get("LFDATA_DATA", DEFAULT_DATA_URI),
        help=f"URI base del almacenamiento (por defecto {DEFAULT_DATA_URI} o $LFDATA_DATA)",
    )
    minutes.add_argument(
        "--mappings",
        default=DEFAULT_MAPPINGS_DIR,
        help=f"Directorio de los ficheros de mappings (por defecto {DEFAULT_MAPPINGS_DIR}/)",
    )
    minutes.add_argument(
        "--out",
        default="crosscheck-sofascore-biwenger-minutes.json",
        help="Ruta del informe JSON",
    )
    minutes.set_defaults(func=_cmd_crosscheck_minutes)

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
        choices=("la-liga",),
        help="Competición cuyos jugadores se sondean (de Biwenger solo la-liga, ADR 0008)",
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
        choices=("la-liga",),
        help="Competición cuyos jugadores se sondean en Biwenger (solo la-liga, ADR 0008)",
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

    newcomers = subparsers.add_parser(
        "newcomers",
        help=(
            "Detecta los fichajes de la plantilla (sin puntos en temporadas anteriores) "
            "y dispara su descarga bajo demanda de Transfermarkt y SofaScore"
        ),
    )
    newcomers.add_argument(
        "--competition",
        default="la-liga",
        choices=("la-liga",),
        help="Competición cuya plantilla se inspecciona (de Biwenger solo la-liga, ADR 0008)",
    )
    newcomers.add_argument(
        "--season",
        type=int,
        required=True,
        help="Año de inicio de la temporada en curso (2025 = 2025/26)",
    )
    newcomers.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo detecta y los lista en el log; no descarga nada ni escribe tablas",
    )
    newcomers.add_argument(
        "--max-newcomers",
        type=int,
        default=None,
        help=(
            "Resuelve como mucho N fichajes en este run y aplaza el resto al siguiente. "
            "Con el histórico de puntos incompleto (backfill a medias) media plantilla parece "
            "recién llegada: el tope evita lanzarle cientos de peticiones a las fuentes"
        ),
    )
    newcomers.add_argument(
        "--since-days",
        type=int,
        default=NEWCOMER_SINCE_DAYS,
        help=(
            "Al refrescar la plantilla de Transfermarkt del club de llegada, no re-pide a los "
            f"jugadores bajados hace menos de N días (por defecto {NEWCOMER_SINCE_DAYS})"
        ),
    )
    newcomers.add_argument(
        "--data",
        default=os.environ.get("LFDATA_DATA", DEFAULT_DATA_URI),
        help=f"URI base del almacenamiento (por defecto {DEFAULT_DATA_URI} o $LFDATA_DATA)",
    )
    newcomers.add_argument(
        "--mappings",
        default=DEFAULT_MAPPINGS_DIR,
        help=f"Directorio de los ficheros de mappings (por defecto {DEFAULT_MAPPINGS_DIR}/)",
    )
    newcomers.set_defaults(func=_cmd_newcomers)

    mapper = subparsers.add_parser(
        "map",
        help="Genera y aplica los mappings Biwenger↔Transfermarkt↔SofaScore (IDs canónicos)",
    )
    mapper.add_argument(
        "--check",
        action="store_true",
        help="No escribe: falla si hay datos de Biwenger sin mapping (CI y pipeline)",
    )
    mapper.add_argument(
        "--season",
        type=int,
        default=DEFAULT_SEASON,
        help=(
            "Temporada de cuyas plantillas salen los clubes: las de Transfermarkt donde buscar "
            "contraparte y el histórico de Biwenger (rounds) que entra en la pasada "
            f"(año de inicio; por defecto {DEFAULT_SEASON})"
        ),
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


def _cmd_ingest_sofascore(args: argparse.Namespace) -> int:
    from lfdata.sources.sofascore import ingest_player
    from lfdata.storage import Storage

    storage = Storage(args.data)
    result = ingest_player(storage, args.player, mappings_dir=args.mappings)
    return _report_ingest(result, args.player, args.data)


def _cmd_backfill_sofascore(args: argparse.Namespace) -> int:
    from lfdata.sources.sofascore import backfill_league_season_for_year
    from lfdata.storage import Storage

    storage = Storage(args.data)
    result = backfill_league_season_for_year(
        storage,
        SOFASCORE_TOURNAMENTS[args.competition],
        args.season,
        max_matches=args.max_matches,
        max_pages=args.max_pages,
        mappings_dir=args.mappings,
    )
    return _report_ingest(result, f"{args.competition} {args.season}", args.data)


def _cmd_curate_sofascore_catalog(args: argparse.Namespace) -> int:
    from lfdata.sources.sofascore import build_catalog
    from lfdata.storage import Storage

    storage = Storage(args.data)
    result = build_catalog(storage)
    return _report_ingest(result, "sofascore-catalog", args.data)


def _cmd_curate_sofascore_canonical(args: argparse.Namespace) -> int:
    from lfdata.sources.sofascore import restamp_canonical
    from lfdata.storage import Storage

    storage = Storage(args.data)
    result = restamp_canonical(storage, mappings_dir=args.mappings)
    return _report_ingest(result, "sofascore-canonical", args.data)


def _cmd_curate_sofascore_matches(args: argparse.Namespace) -> int:
    from lfdata.sources.sofascore import rebuild_matches
    from lfdata.storage import Storage

    storage = Storage(args.data)
    result = rebuild_matches(storage, mappings_dir=args.mappings)
    return _report_ingest(result, "sofascore-matches", args.data)


def _cmd_crosscheck_minutes(args: argparse.Namespace) -> int:
    from pathlib import Path

    from lfdata.sources.sofascore import crossvalidate_minutes
    from lfdata.storage import Storage

    storage = Storage(args.data)
    report = crossvalidate_minutes(storage, mappings_dir=args.mappings)
    out_path = Path(args.out)
    report.save(out_path)
    print(report.summary())
    print(f"Informe en {out_path}")
    # "por debajo del umbral" da código 1 para que un run desatendido lo detecte;
    # 0 filas comunes no es un fallo del cruce (falta el matching), así que no.
    return 1 if report.common_rows > 0 and not report.passes else 0


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


def _cmd_newcomers(args: argparse.Namespace) -> int:
    from lfdata.newcomers import ingest_newcomers
    from lfdata.storage import Storage

    storage = Storage(args.data)
    result = ingest_newcomers(
        storage,
        args.competition,
        args.season,
        mappings_dir=args.mappings,
        since_days=args.since_days,
        max_newcomers=args.max_newcomers,
        dry_run=args.dry_run,
    )
    return _report_ingest(result, f"{args.competition} {args.season}", args.data)


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

        report = run_map(storage, args.mappings, season=args.season)
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
