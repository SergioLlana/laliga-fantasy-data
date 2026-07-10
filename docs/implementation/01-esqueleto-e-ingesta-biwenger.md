# Paso 1 — Esqueleto del proyecto e ingesta de Biwenger

**Objetivo:** paquete instalable con CLI que descarga La Liga y Segunda División de Biwenger (temporada actual e históricas) a la capa cruda y produce las primeras tablas curadas.

## Decisiones ya tomadas que aplican aquí

- Paquete `lfdata` en `src/`, gestionado con `uv` (Python ≥3.12), tests con `pytest`, linter `ruff`.
- Almacenamiento con dos capas, cruda y curada (ADR 0003). El destino es una URI base: `file://./data` en desarrollo, `s3://lfdata-data-593760774245` en producción (perfil AWS `lfdata`, región `eu-south-2`).
- Transporte HTTP común con `curl-cffi` (impersonación de Chrome), necesario para SofaScore/FotMob y usado por coherencia en todas las fuentes.

## Componentes

### `lfdata.sources.http` — transporte común

- Sesión `curl-cffi` con: espera configurable entre peticiones por fuente (Biwenger: 2 s), reintentos con espera creciente ante 429/5xx, y User-Agent realista.
- Modo proxy opcional (ScrapeOps, plan gratuito): si `LFDATA_SCRAPEOPS_KEY` está definida, las peticiones de las fuentes marcadas pasan por el proxy. Apagado por defecto.
- Toda respuesta se escribe en `raw/` **antes** de intentar interpretarla.

### `lfdata.storage` — capas de datos

- `RawStore`: guarda bytes con clave `raw/{fuente}/{dataset}/fecha_descarga=YYYY-MM-DD/{nombre}.json`.
- `CuratedStore`: lee/escribe Parquet por nombre de tabla. Una implementación, dos backends (sistema de ficheros local y S3 vía `s3fs` o `boto3`).

### `lfdata.sources.biwenger` — cliente e intérprete

Endpoints (verificados el 2026-07-07, ver `docs/experiments/2026-07-07-alex-fores.md`):

- `competitions/{la-liga|segunda-division}/data?lang=es&score=1` → plantilla completa de la competición: jugadores (precio, incremento, posición, estado, **puntos acumulados**), equipos y jornadas. Ignora el parámetro `season`: siempre devuelve la temporada actual.
- `players/{competición}/{slug}?fields=*,reports(points,home,status,match(*,round),rawStats),prices,seasons&season=YYYY` → por jugador y temporada: un report por partido con puntos en los cinco sistemas de puntuación, minutos, nota SofaScore (solo La Liga), y precios diarios. Solo sirve a jugadores presentes en la plantilla actual; los que dejaron la competición devuelven 404.
- `rounds/{competición}/{round_id}?score=N` (verificado el 2026-07-10) → los partidos de una jornada con la lista de **todos** los jugadores que puntuaron (incluidos los que ya no están en la competición), sus eventos y sus puntos bajo el sistema `N`. Sin `rawStats` (ni minutos ni nota). Los `round_id` de temporadas pasadas se obtienen de los reports de cualquier jugador veterano y siguen siendo accesibles.

Validación de estructura con modelos Pydantic: si falta un campo esperado o cambia el tipo, el ingestor falla con error explícito (la fuente cambió su formato), nunca escribe una tabla curada a medias.

### Tablas curadas que produce este paso

| Tabla | Grano | Campos principales |
|---|---|---|
| `biwenger_players` | jugador | id, slug, nombre, fecha de nacimiento, posición, equipo actual, estado |
| `biwenger_teams` | equipo | id, slug, nombre, competición |
| `biwenger_rounds` | jornada | id, nombre, temporada, competición, estado |
| `fantasy_points` | jugador-partido | ids Biwenger de jugador/partido/jornada, temporada, competición, puntos en los 5 sistemas, minutos, nota sofascore (si existe), local/visitante, resultado |
| `fantasy_round_points` | jugador-partido | ids Biwenger de jugador/equipo/partido/jornada, temporada, competición, puntos en los 5 sistemas, local/visitante, marcador, resultado. Igual que `fantasy_points` pero **sin minutos ni nota** y con *todos* los jugadores de cada jornada, incluidos los que ya dejaron la competición (el detalle por jugador da 404) |
| `biwenger_prices` | jugador-día | id jugador, fecha, precio, temporada |

Nota: estas tablas llevan aún IDs de Biwenger. El ID canónico llega en el paso 2; a partir de entonces `fantasy_points` y `biwenger_prices` se publican con ambos.

### CLI

```
lfdata ingest biwenger --competition la-liga --season 2026        # temporada actual
lfdata ingest biwenger --competition segunda-division --season 2026
lfdata backfill biwenger --competition la-liga --from-season 2022 # históricas, con espera larga
# Puntos por jornada de todos los jugadores (histórico sin sesgo de supervivencia).
# Descubre las jornadas de la temporada solo; --resume salta las ya curadas.
lfdata ingest biwenger-rounds --competition la-liga --season 2025 [--resume]
```

## Orden de trabajo

1. `pyproject.toml`, estructura `src/lfdata`, CLI vacío, CI de GitHub Actions (lint + tests).
2. `storage` con backend local + tests.
3. `sources.http` + tests (falsificando el transporte, sin red).
4. Cliente Biwenger contra fixtures reales guardadas del experimento; después, ingesta real de la temporada actual.
5. Backend S3 y primera escritura al bucket real.
6. Backfill de 5 temporadas de La Liga y Segunda. Dos vías complementarias (decidido el 2026-07-10):
   - **Detalle por jugador actual** (~630 × temporada): rawStats completo (minutos, nota, precios), pero solo cubre a los jugadores que siguen en la competición (los que se fueron devuelven 404 → sesgo de supervivencia).
   - **Rounds históricos** (38 jornadas × 5 sistemas ≈ 190 peticiones por temporada): puntos por sistema de *todos* los jugadores de cada jornada pasada, incluidos los que se fueron, a una tabla curada propia (sin minutos ni nota; el eventing de esos jugadores lo aporta el backfill de SofaScore del paso 3).
   El run es reanudable: una temporada pasada es inmutable, así que la marca de reanudación es la mera existencia del raw por jugador-temporada (patrón `--since-days` de Transfermarkt, con antigüedad infinita). Se ejecuta por ScrapeOps dentro del mes de pago puntual, sincronizado con el backfill de SofaScore (ADR 0004).

## Hecho cuando

- `lfdata ingest biwenger` deja la temporada actual completa en `raw/` y `curated/`, en local y en S3.
- Las 5 temporadas históricas de La Liga y Segunda están en el bucket.
- Un test de contrato falla si Biwenger cambia la forma de la respuesta.
