"""Capa de identidad: IDs canónicos y mappings Biwenger↔Transfermarkt (ADR 0001)."""

from lfdata.mappings.run import MapReport, UnappliedDecision, check_mappings, run_map
from lfdata.mappings.store import MappingIntegrityError, MappingStore

__all__ = [
    "MapReport",
    "MappingIntegrityError",
    "MappingStore",
    "UnappliedDecision",
    "check_mappings",
    "run_map",
]
