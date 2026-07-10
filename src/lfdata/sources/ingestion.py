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
    """Filas escritas por tabla y los jugadores que fallaron durante el run.

    ``anomalies`` cuenta, por motivo, los datos que la fuente sirvió incompletos y
    no llegaron a las tablas curadas (p. ej. reports de Biwenger con puntos pero
    sin ``rawStats``): no abortan el run, pero se cuentan para que el resumen no
    los silencie.

    ``stats`` lleva métricas del run que no son filas de una tabla ni anomalías
    (p. ej. jugadores refrescados vs. saltados en el refresh por deltas): el
    resumen del CLI las imprime para dar visibilidad de qué hizo el run.
    """

    rows: dict[str, int] = field(default_factory=dict)
    failures: list[PlayerFailure] = field(default_factory=list)
    anomalies: dict[str, int] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)

    def merge(self, other: IngestResult) -> IngestResult:
        """Une dos resultados (suma tablas distintas, concatena fallos)."""
        anomalies = dict(self.anomalies)
        for reason, count in other.anomalies.items():
            anomalies[reason] = anomalies.get(reason, 0) + count
        stats = dict(self.stats)
        for name, count in other.stats.items():
            stats[name] = stats.get(name, 0) + count
        return IngestResult(
            rows={**self.rows, **other.rows},
            failures=[*self.failures, *other.failures],
            anomalies=anomalies,
            stats=stats,
        )

    __or__ = merge
    __ior__ = merge
