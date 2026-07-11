"""Cruce de minutos SofaScore vs Biwenger para La Liga y LaLiga2.

Valida la ingesta de eventing contra una fuente independiente: los minutos de
SofaScore (``player_match_stats``) deben cuadrar con los de Biwenger
(``fantasy_points``) en las filas comunes. El plan fija la tolerancia en ±10% y
exige que se cumpla en al menos el 95% de las filas comunes (docs/implementation/03).

El emparejamiento de jugador es por ``canonical_id`` (ADR 0001): a SofaScore ya
lo trae la fila; a Biwenger se le resuelve desde ``player_mappings``. El
emparejamiento de partido es por fecha —un jugador disputa como mucho un partido
al día—, así que la clave común es ``(canonical_id, date)``.

Mientras no existan mappings aprobados, el informe se genera igualmente con 0
filas comunes: la maquinaria queda lista para cuando el matching (paso 3, orden
de trabajo 5) puebla los ``canonical_id``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from lfdata.mappings import MappingStore
from lfdata.storage import Storage

logger = logging.getLogger(__name__)

BIWENGER = "biwenger"
SOFASCORE = "sofascore"
DEFAULT_TOLERANCE = 0.10
# Cobertura del cruce: las ligas donde ambas fuentes solapan (Biwenger cubre La
# Liga y Segunda). ut 8 = LaLiga, ut 54 = LaLiga2 en SofaScore.
COVERED_COMPETITIONS = ("8", "54")


@dataclass
class CrossCheckReport:
    tolerance: float
    common_rows: int
    within_tolerance: int
    discrepancies: list[dict] = field(default_factory=list)

    @property
    def pct_within(self) -> float:
        if self.common_rows == 0:
            return 0.0
        return self.within_tolerance / self.common_rows

    @property
    def passes(self) -> bool:
        """El cruce se da por bueno con ≥95% de filas dentro de tolerancia."""
        return self.common_rows > 0 and self.pct_within >= 0.95

    def summary(self) -> str:
        if self.common_rows == 0:
            return (
                "Cruce SofaScore↔Biwenger: 0 filas comunes (aún sin canonical_id en "
                "ambas fuentes; ejecuta el matching). Informe generado igualmente."
            )
        pct = self.pct_within * 100
        verdict = "OK" if self.passes else "POR DEBAJO DEL UMBRAL"
        return (
            f"Cruce SofaScore↔Biwenger: {self.within_tolerance}/{self.common_rows} filas "
            f"con minutos dentro de ±{self.tolerance * 100:.0f}% ({pct:.1f}%) — {verdict}. "
            f"{len(self.discrepancies)} discrepancias."
        )

    def save(self, path: Path) -> None:
        payload = {
            "tolerance": self.tolerance,
            "common_rows": self.common_rows,
            "within_tolerance": self.within_tolerance,
            "pct_within": self.pct_within,
            "passes": self.passes,
            "discrepancies": self.discrepancies,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def crossvalidate_minutes(
    storage: Storage,
    *,
    mappings_dir: str = "mappings",
    tolerance: float = DEFAULT_TOLERANCE,
) -> CrossCheckReport:
    """Compara minutos SofaScore vs Biwenger por (canonical_id, fecha)."""
    sofascore = _sofascore_minutes(storage)
    biwenger = _biwenger_minutes(storage, mappings_dir)
    if sofascore.empty or biwenger.empty:
        return CrossCheckReport(tolerance=tolerance, common_rows=0, within_tolerance=0)

    merged = sofascore.merge(biwenger, on=["canonical_id", "date"], suffixes=("_so", "_bi"))
    merged = merged[(merged["minutes_bi"] > 0) & merged["minutes_so"].notna()]
    if merged.empty:
        return CrossCheckReport(tolerance=tolerance, common_rows=0, within_tolerance=0)

    delta = (merged["minutes_so"] - merged["minutes_bi"]).abs()
    within = delta <= tolerance * merged["minutes_bi"]
    discrepancies = [
        {
            "canonical_id": row["canonical_id"],
            "date": row["date"],
            "minutes_sofascore": int(row["minutes_so"]),
            "minutes_biwenger": int(row["minutes_bi"]),
        }
        for _, row in merged[~within].iterrows()
    ]
    return CrossCheckReport(
        tolerance=tolerance,
        common_rows=int(len(merged)),
        within_tolerance=int(within.sum()),
        discrepancies=discrepancies,
    )


def _sofascore_minutes(storage: Storage) -> pd.DataFrame:
    """Minutos de SofaScore por (canonical_id, fecha) en las ligas cubiertas."""
    try:
        table = storage.curated.read_table("player_match_stats")
    except FileNotFoundError:
        return pd.DataFrame(columns=["canonical_id", "date", "minutes"])
    table = table[
        (table["source"] == SOFASCORE)
        & table["competition"].isin(COVERED_COMPETITIONS)
        & table["canonical_id"].astype(str).str.len().gt(0)
    ]
    return table[["canonical_id", "date", "minutes"]].dropna(subset=["date"])


def _biwenger_minutes(storage: Storage, mappings_dir: str) -> pd.DataFrame:
    """Minutos de Biwenger por (canonical_id, fecha), vía player_mappings."""
    try:
        table = storage.curated.read_table("fantasy_points")
    except FileNotFoundError:
        return pd.DataFrame(columns=["canonical_id", "date", "minutes"])
    store = MappingStore(Path(mappings_dir))
    store.load()
    biwenger_to_canonical = MappingStore.canonical_by_source(store.players, BIWENGER)
    if not biwenger_to_canonical:
        return pd.DataFrame(columns=["canonical_id", "date", "minutes"])
    table = table.copy()
    table["canonical_id"] = table["player_id"].astype(str).map(biwenger_to_canonical)
    table = table[table["canonical_id"].notna()]
    return table[["canonical_id", "date", "minutes"]].dropna(subset=["date"])
