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
from lfdata.sources.sofascore.ingest import ingest_player

__all__ = [
    "API_BASE",
    "PROXY_OVERFLOW",
    "WAIT_SECONDS",
    "SofaScoreClient",
    "SourceFormatError",
    "ingest_player",
]
