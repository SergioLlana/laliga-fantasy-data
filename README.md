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
```

Los datos se escriben en dos capas bajo la URI de `--data` (por defecto `file://./data`, configurable con `$LFDATA_DATA`): la respuesta cruda tal cual en `raw/` y tablas Parquet en `curated/` (`biwenger_players`, `biwenger_teams`), legibles con pandas o DuckDB.

## Desarrollo

```bash
uv run pytest              # tests
uv run ruff check .        # lint
uv run ruff format .       # formateo
```

La documentación del proyecto vive en `docs/`: el plan general en `docs/plan.md`, los planes de implementación en `docs/implementation/` y las decisiones de arquitectura en `docs/adr/`. El lenguaje del dominio está en `CONTEXT.md`.
