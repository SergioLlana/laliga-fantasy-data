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

# Ingesta de la plantilla de Biwenger (solo la-liga: sus ids cambian por
# competición y romperían los mappings — ADR 0008; Segunda y Copa se cubren
# con Transfermarkt/SofaScore como ligas de origen)
uv run lfdata ingest biwenger --competition la-liga

# Ingesta de Transfermarkt: plantillas por club, perfiles, valores y traspasos
# (espera 4 s entre peticiones; --max-clubs limita el recorrido para una prueba)
uv run lfdata ingest transfermarkt --competition la-liga
uv run lfdata ingest transfermarkt --competition segunda-division --max-clubs 2

# --since-days no vuelve a pedir a la fuente al jugador bajado hace menos de N días,
# pero lo cura igual desde raw/: reconstruye la tabla sin re-scrapear
uv run lfdata ingest transfermarkt --competition la-liga --season 2026 --since-days 30

# Historial de un jugador fuera de plantilla (los sin-candidato enlazados a mano, o
# un fichaje de Segunda/extranjero que aún no sale en ningún kader): cura solo sus
# cuatro tablas de historial (valores, traspasos, disponibilidad, lesiones), nunca
# transfermarkt_players (ADR 0013). Por spieler_id, URL de perfil o canonical_id
uv run lfdata ingest transfermarkt-player --player 709380
uv run lfdata ingest transfermarkt-player --player p00042 --cached   # re-cura desde raw/

# Valor total de plantilla por club (una petición por liga-temporada) de las 7 ligas
# (La Liga, Segunda, Premier, Serie A, Bundesliga, Ligue 1 y Primeira): el nivel de
# equipo del modelo y, promediando por liga, el nivel de liga del baseline de fichajes.
# Los clubes de La Liga/Segunda se resuelven a canónico; los extranjeros conservan su id
uv run lfdata ingest transfermarkt-values --season 2025
uv run lfdata ingest transfermarkt-values --season 2025 --league premier-league
uv run lfdata ingest transfermarkt-values --season 2025 --cached   # re-cura desde raw/

# Mappings a IDs canónicos (Biwenger↔Transfermarkt↔SofaScore): aprueba los seguros
# y deja los dudosos en mappings/*-review.csv para decidirlos a mano (ver
# mappings/README.md). --season es la temporada de cuyas plantillas salen los
# clubes, no un filtro de a quién se puede mapear: la contraparte se busca en todas
# las descargadas. SofaScore se cuelga del canónico que Biwenger obtuvo de
# Transfermarkt (necesita antes el catálogo, ver más abajo)
uv run lfdata map --season 2026
uv run lfdata map --check   # falla si algo de Biwenger o del eventing quedó sin canonical_id

# Ingesta bajo demanda de SofaScore: historial completo de un jugador de
# cualquier liga (por nombre, id de SofaScore o canonical_id ya mapeado)
uv run lfdata ingest sofascore --player "Alex Fores"
uv run lfdata ingest sofascore --player 1086128

# Backfill de SofaScore por liga-temporada (calendario → alineaciones); reanudable,
# --max-matches acota una prueba (--season es el año de inicio: 2025 = 2025/26)
uv run lfdata backfill sofascore --competition la-liga --season 2025 --max-matches 5

# Copa del Rey y competiciones UEFA (Champions, Europa League, Conference): baja solo
# los partidos con equipos de La Liga y cura fixtures (calendario) + cup_minutes (minutos
# entre semana), la densidad de calendario del modelo de minutos. Requiere el catálogo de
# La Liga ya construido (sabe quiénes son "los nuestros" por sofascore_teams)
uv run lfdata backfill sofascore-cups --competition champions-league --season 2025
uv run lfdata backfill sofascore-cups --competition copa-del-rey --season 2025 --max-matches 5

# Catálogo de identidad de SofaScore desde raw/ (alineaciones + calendario), sin
# peticiones: publica sofascore_players y sofascore_teams, la evidencia que lee
# `lfdata map`. Se corre tras el backfill y antes del map
uv run lfdata curate sofascore-catalog

# Tras aprobar mappings nuevos, re-estampa el canonical_id en el eventing ya
# curado cruzándolo con los mappings (sin releer raw/)
uv run lfdata curate sofascore-canonical

# Re-cura fixtures y cup_minutes desde raw/ (calendario de liga + copas), sin peticiones
uv run lfdata curate sofascore-cups

# Detector de fichajes: quien está en la plantilla sin puntos en temporadas
# anteriores de esa competición (el ascendido de Segunda incluido: es una liga de
# origen más). Refresca la plantilla de Transfermarkt de su club de llegada, lo
# mapea (o lo encola a revisión) y descarga su historial de SofaScore
uv run lfdata newcomers --competition la-liga --season 2026
uv run lfdata newcomers --season 2026 --dry-run          # solo los lista, sin descargar
uv run lfdata newcomers --season 2026 --max-newcomers 5  # tope de fichajes por run

# Informe de cruce de minutos SofaScore ↔ Biwenger (tolerancia 10 pp, umbral 95)
uv run lfdata crosscheck sofascore-biwenger-minutes --out crosscheck.json
```

Los datos se escriben en dos capas bajo la URI de `--data` (por defecto `file://./data`, configurable con `$LFDATA_DATA`): la respuesta cruda tal cual en `raw/` y tablas Parquet en `curated/`, legibles con pandas o DuckDB. Biwenger produce `biwenger_players` (solo jugadores: los entrenadores, que Biwenger publica en la misma lista con ficha y precio, quedan fuera) y `biwenger_teams`; Transfermarkt produce `transfermarkt_players` (particionada por competición **y temporada**: la pertenencia a una plantilla es de una temporada concreta, así que ingerir 2023 no toca a los jugadores de 2026), `market_values_tm`, `transfers`, `availability_tm` (disponibilidad por partido) e `injuries_tm` (historial de lesiones) —estas cuatro son el histórico del jugador, el mismo desde cualquier temporada, y van solo por competición—, aún con IDs de Transfermarkt, a la espera del paso de mapping a IDs canónicos. SofaScore produce `player_season_stats` (agregado de 115 campos por jugador-temporada) y `player_match_stats` (nota y métricas de evento por jugador-partido: minutos, pases, remates, goles, asistencias, xG…); cada fila lleva su `canonical_id` si el id de SofaScore ya está mapeado y, si no, vacío hasta que `lfdata map` (sobre el catálogo `sofascore_players`) proponga el canónico y `lfdata curate sofascore-canonical` lo re-estampe. El catálogo de identidad de SofaScore (`sofascore_players`, `sofascore_teams`) se reconstruye desde `raw/` sin peticiones con `lfdata curate sofascore-catalog`. De la Copa del Rey y las competiciones UEFA (Champions, Europa League, Conference) SofaScore produce, solo para los equipos de La Liga, `fixtures` (un partido por fila con fecha y ambos equipos, incluida la liga: la densidad de calendario en una sola tabla) y `cup_minutes` (minutos por jugador-partido entre semana, con `canonical_id`); su `raw/` vive en datasets propios (`cup-events`, `cup-lineups`) para no mezclarse con el eventing ni con el catálogo de identidad. Transfermarkt produce además `squad_values` (valor total de plantilla por club-temporada de las 7 ligas cubiertas, con `canonical_team_id` para los clubes de La Liga/Segunda): el nivel de equipo del modelo de rendimiento y, promediado por liga, el nivel de liga del baseline de fichajes. El detector de fichajes produce `newcomers` (grano jugador-temporada de debut: quién llegó, a qué equipo y si su historial está descargado), que es además su marca de idempotencia: un fichaje ya resuelto no vuelve a generar peticiones.

## Desarrollo

```bash
uv run pytest              # tests
uv run ruff check .        # lint
uv run ruff format .       # formateo
```

El orden del backfill de temporadas pasadas y las cadencias de actualización durante la temporada están en [`docs/runbook.md`](docs/runbook.md).

La documentación del proyecto vive en `docs/`: el plan general en `docs/plan.md`, los planes de implementación en `docs/implementation/` y las decisiones de arquitectura en `docs/adr/`. El lenguaje del dominio está en `CONTEXT.md`.

## Infraestructura

La infraestructura AWS (`eu-south-2`) se define con Terraform en `infra/` y se aplica a mano con credenciales de administrador. El núcleo incluye el bucket de datos, el usuario CLI de permisos mínimos, el rol del pipeline, el repositorio ECR y la alerta de presupuesto. Ver [`infra/README.md`](infra/README.md) para el procedimiento de arranque.
