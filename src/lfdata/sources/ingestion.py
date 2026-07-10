"""Resultado común de una ingesta resiliente (issue #36).

Una ingesta larga (cientos o miles de peticiones) sobrevive a fallos puntuales:
un jugador que la fuente ya no sirve (p. ej. 404) se registra como
``PlayerFailure`` y se salta, y el run continúa. ``IngestResult`` reúne las
filas escritas por tabla y esos fallos, para que el CLI imprima un resumen y
devuelva un código de salida distinto de 0 cuando alguno se produjo.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlayerFailure:
    """Un jugador que no se pudo curar por un fallo HTTP no reintentable."""

    player: str
    url: str
    status: int

    def __str__(self) -> str:
        return f"{self.player}: HTTP {self.status} al pedir {self.url}"


@dataclass
class IngestResult:
    """Filas escritas por tabla y los jugadores que fallaron durante el run."""

    rows: dict[str, int] = field(default_factory=dict)
    failures: list[PlayerFailure] = field(default_factory=list)

    def merge(self, other: IngestResult) -> IngestResult:
        """Une dos resultados (suma tablas distintas, concatena fallos)."""
        return IngestResult(
            rows={**self.rows, **other.rows},
            failures=[*self.failures, *other.failures],
        )

    __or__ = merge
    __ior__ = merge
