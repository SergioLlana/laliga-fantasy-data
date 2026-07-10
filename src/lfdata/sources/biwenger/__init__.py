"""Cliente e intérprete de Biwenger (API pública cf.biwenger.com)."""

from lfdata.sources.biwenger.client import COMPETITIONS, BiwengerClient, SourceFormatError
from lfdata.sources.biwenger.ingest import (
    RoundDiscoveryError,
    ingest_reports,
    ingest_rounds,
    ingest_squad,
)

__all__ = [
    "COMPETITIONS",
    "BiwengerClient",
    "RoundDiscoveryError",
    "SourceFormatError",
    "ingest_reports",
    "ingest_rounds",
    "ingest_squad",
]
