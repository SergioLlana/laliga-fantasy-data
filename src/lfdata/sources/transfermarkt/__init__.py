"""Cliente e intérprete de Transfermarkt (HTML + JSON interno `ceapi`)."""

from lfdata.sources.transfermarkt.client import (
    COMPETITIONS,
    SQUAD_VALUE_LEAGUES,
    TransfermarktClient,
)
from lfdata.sources.transfermarkt.ingest import (
    BAJO_DEMANDA,
    DEFAULT_SEASON,
    HISTORY_TABLES,
    SQUAD_VALUES_TABLE,
    ingest_clubs,
    ingest_player,
    ingest_squad_values,
    ingest_squads,
)
from lfdata.sources.transfermarkt.parse import SourceFormatError

__all__ = [
    "BAJO_DEMANDA",
    "COMPETITIONS",
    "DEFAULT_SEASON",
    "HISTORY_TABLES",
    "SQUAD_VALUE_LEAGUES",
    "SQUAD_VALUES_TABLE",
    "SourceFormatError",
    "TransfermarktClient",
    "ingest_clubs",
    "ingest_player",
    "ingest_squad_values",
    "ingest_squads",
]
