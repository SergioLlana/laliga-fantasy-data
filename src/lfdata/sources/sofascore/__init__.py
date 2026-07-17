"""Fuente SofaScore: eventing por jugador-partido (ADR 0002).

Acceso por API no oficial con impersonación de Chrome (curl-cffi). De momento el
modo bajo demanda: historial completo de un jugador de cualquier liga.
"""

from lfdata.sources.sofascore.catalog import build_catalog, restamp_canonical
from lfdata.sources.sofascore.client import (
    ALL_TOURNAMENTS,
    API_BASE,
    CALENDAR_TOURNAMENTS,
    COMPETITION_BY_TOURNAMENT,
    PROXY_OVERFLOW,
    TOURNAMENTS,
    WAIT_SECONDS,
    SofaScoreClient,
    SourceFormatError,
)
from lfdata.sources.sofascore.crossvalidate import CrossCheckReport, crossvalidate_minutes
from lfdata.sources.sofascore.cups import backfill_cups_for_year, rebuild_cups
from lfdata.sources.sofascore.ingest import (
    SearchIdentity,
    backfill_league_season,
    backfill_league_season_for_year,
    ingest_player,
    rebuild_matches,
    resolve_identity_by_search,
    resolve_season_id,
    season_year_label,
)

__all__ = [
    "ALL_TOURNAMENTS",
    "API_BASE",
    "CALENDAR_TOURNAMENTS",
    "COMPETITION_BY_TOURNAMENT",
    "PROXY_OVERFLOW",
    "TOURNAMENTS",
    "WAIT_SECONDS",
    "CrossCheckReport",
    "SearchIdentity",
    "SofaScoreClient",
    "SourceFormatError",
    "backfill_cups_for_year",
    "backfill_league_season",
    "backfill_league_season_for_year",
    "build_catalog",
    "crossvalidate_minutes",
    "ingest_player",
    "rebuild_cups",
    "rebuild_matches",
    "resolve_identity_by_search",
    "resolve_season_id",
    "restamp_canonical",
    "season_year_label",
]
