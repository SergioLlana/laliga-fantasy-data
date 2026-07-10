"""Capa de identidad: IDs canónicos y mappings Biwenger↔Transfermarkt (ADR 0001)."""

from lfdata.mappings.run import MapReport, check_mappings, run_map
from lfdata.mappings.store import MappingStore

__all__ = ["MapReport", "MappingStore", "check_mappings", "run_map"]
