"""Fuente SofaScore: eventing por jugador-partido (ADR 0002).

Acceso por API no oficial con impersonación de Chrome (curl-cffi). De momento el
modo bajo demanda: historial completo de un jugador de cualquier liga.
"""

from lfdata.sources.sofascore.client import (
    API_BASE,
    PROXY_OVERFLOW,
    WAIT_SECONDS,
    SofaScoreClient,
    SourceFormatError,
)
from lfdata.sources.sofascore.crossvalidate import CrossCheckReport, crossvalidate_minutes
from lfdata.sources.sofascore.ingest import (
    backfill_league_season,
    backfill_league_season_for_year,
    ingest_player,
    resolve_season_id,
    season_year_label,
)

__all__ = [
    "API_BASE",
    "PROXY_OVERFLOW",
    "WAIT_SECONDS",
    "CrossCheckReport",
    "SofaScoreClient",
    "SourceFormatError",
    "backfill_league_season",
    "backfill_league_season_for_year",
    "crossvalidate_minutes",
    "ingest_player",
    "resolve_season_id",
    "season_year_label",
]
