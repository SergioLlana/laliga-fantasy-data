"""Cliente e intérprete de Transfermarkt (HTML + JSON interno `ceapi`)."""

from lfdata.sources.transfermarkt.client import COMPETITIONS, TransfermarktClient
from lfdata.sources.transfermarkt.ingest import DEFAULT_SEASON, ingest_squads
from lfdata.sources.transfermarkt.parse import SourceFormatError

__all__ = [
    "COMPETITIONS",
    "DEFAULT_SEASON",
    "SourceFormatError",
    "TransfermarktClient",
    "ingest_squads",
]
