"""Cliente e intérprete de Transfermarkt (HTML + JSON interno `ceapi`)."""

from lfdata.sources.transfermarkt.client import (
    COMPETITIONS,
    SQUAD_VALUE_LEAGUES,
    TransfermarktClient,
)
from lfdata.sources.transfermarkt.ingest import (
    DEFAULT_SEASON,
    SQUAD_VALUES_TABLE,
    ingest_clubs,
    ingest_squad_values,
    ingest_squads,
)
from lfdata.sources.transfermarkt.parse import SourceFormatError

__all__ = [
    "COMPETITIONS",
    "DEFAULT_SEASON",
    "SQUAD_VALUE_LEAGUES",
    "SQUAD_VALUES_TABLE",
    "SourceFormatError",
    "TransfermarktClient",
    "ingest_clubs",
    "ingest_squad_values",
    "ingest_squads",
]
