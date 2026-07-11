"""Cliente e intérprete de Biwenger (API pública cf.biwenger.com)."""

from lfdata.sources.biwenger.client import COMPETITIONS, BiwengerClient, SourceFormatError
from lfdata.sources.biwenger.ingest import (
    RoundDiscoveryError,
    ingest_reports,
    ingest_reports_delta,
    ingest_rounds,
    ingest_squad,
)
from lfdata.sources.biwenger.probe import (
    ProbeReport,
    default_out_path,
    probe_quota_window,
    run_probe,
)

__all__ = [
    "COMPETITIONS",
    "BiwengerClient",
    "ProbeReport",
    "RoundDiscoveryError",
    "SourceFormatError",
    "default_out_path",
    "ingest_reports",
    "ingest_reports_delta",
    "ingest_rounds",
    "ingest_squad",
    "probe_quota_window",
    "run_probe",
]
