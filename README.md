# La Liga Fantasy Data

Plataforma abierta de datos y proyecciones para fantasy de La Liga (Biwenger primero): proyecciones de puntos, recomendaciones de mercado y optimización de alineación.

## Instalación

Requiere [uv](https://docs.astral.sh/uv/) y Python ≥ 3.12.

```bash
git clone https://github.com/SergioLlana/laliga-fantasy-data.git
cd laliga-fantasy-data
uv sync --dev
```

## Uso

```bash
uv run lfdata --help

# Ingesta de la plantilla de una competición de Biwenger
uv run lfdata ingest biwenger --competition la-liga
uv run lfdata ingest biwenger --competition segunda-division

# Ingesta de Transfermarkt: plantillas por club, perfiles, valores y traspasos
# (espera 4 s entre peticiones; --max-clubs limita el recorrido para una prueba)
uv run lfdata ingest transfermarkt --competition la-liga
uv run lfdata ingest transfermarkt --competition segunda-division --max-clubs 2
```

Los datos se escriben en dos capas bajo la URI de `--data` (por defecto `file://./data`, configurable con `$LFDATA_DATA`): la respuesta cruda tal cual en `raw/` y tablas Parquet en `curated/`, legibles con pandas o DuckDB. Biwenger produce `biwenger_players` y `biwenger_teams`; Transfermarkt produce `transfermarkt_players`, `market_values_tm` y `transfers` (aún con IDs de Transfermarkt, a la espera del paso de mapping a IDs canónicos).

## Desarrollo

```bash
uv run pytest              # tests
uv run ruff check .        # lint
uv run ruff format .       # formateo
```

La documentación del proyecto vive en `docs/`: el plan general en `docs/plan.md`, los planes de implementación en `docs/implementation/` y las decisiones de arquitectura en `docs/adr/`. El lenguaje del dominio está en `CONTEXT.md`.
