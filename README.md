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

# Ingesta bajo demanda de SofaScore: historial completo de un jugador de
# cualquier liga (por nombre, id de SofaScore o canonical_id ya mapeado)
uv run lfdata ingest sofascore --player "Alex Fores"
uv run lfdata ingest sofascore --player 1086128
```

Los datos se escriben en dos capas bajo la URI de `--data` (por defecto `file://./data`, configurable con `$LFDATA_DATA`): la respuesta cruda tal cual en `raw/` y tablas Parquet en `curated/`, legibles con pandas o DuckDB. Biwenger produce `biwenger_players` y `biwenger_teams`; Transfermarkt produce `transfermarkt_players`, `market_values_tm`, `transfers`, `availability_tm` (disponibilidad por partido) e `injuries_tm` (historial de lesiones), aún con IDs de Transfermarkt, a la espera del paso de mapping a IDs canónicos. SofaScore produce `player_season_stats` (agregado de 115 campos por jugador-temporada) y `player_match_stats` (nota y métricas de evento por jugador-partido: minutos, pases, remates, goles, asistencias, xG…); cada fila lleva su `canonical_id` si el id de SofaScore ya está mapeado y, si no, el jugador queda encolado en `mappings/sofascore-review.csv`.

## Desarrollo

```bash
uv run pytest              # tests
uv run ruff check .        # lint
uv run ruff format .       # formateo
```

La documentación del proyecto vive en `docs/`: el plan general en `docs/plan.md`, los planes de implementación en `docs/implementation/` y las decisiones de arquitectura en `docs/adr/`. El lenguaje del dominio está en `CONTEXT.md`.

## Infraestructura

La infraestructura AWS (`eu-south-2`) se define con Terraform en `infra/` y se aplica a mano con credenciales de administrador. El núcleo incluye el bucket de datos, el usuario CLI de permisos mínimos, el rol del pipeline, el repositorio ECR y la alerta de presupuesto. Ver [`infra/README.md`](infra/README.md) para el procedimiento de arranque.
