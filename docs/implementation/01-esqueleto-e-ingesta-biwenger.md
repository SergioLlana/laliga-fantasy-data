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

- `competitions/{la-liga|segunda-division}/data?lang=es&score=1` → plantilla completa de la competición: jugadores (precio, incremento, posición, estado), equipos y jornadas.
- `players/{competición}/{slug}?fields=*,reports(points,home,status,match(*,round),rawStats),prices,seasons&season=YYYY` → por jugador y temporada: un report por partido con puntos en los cinco sistemas de puntuación, minutos, nota SofaScore (solo La Liga), y precios diarios.

Validación de estructura con modelos Pydantic: si falta un campo esperado o cambia el tipo, el ingestor falla con error explícito (la fuente cambió su formato), nunca escribe una tabla curada a medias.

### Tablas curadas que produce este paso

| Tabla | Grano | Campos principales |
|---|---|---|
| `biwenger_players` | jugador | id, slug, nombre, fecha de nacimiento, posición, equipo actual, estado |
| `biwenger_teams` | equipo | id, slug, nombre, competición |
| `biwenger_rounds` | jornada | id, nombre, temporada, competición, estado |
| `fantasy_points` | jugador-partido | ids Biwenger de jugador/partido/jornada, temporada, competición, puntos en los 5 sistemas, minutos, nota sofascore (si existe), local/visitante, resultado |
| `biwenger_prices` | jugador-día | id jugador, fecha, precio, temporada |

Nota: estas tablas llevan aún IDs de Biwenger. El ID canónico llega en el paso 2; a partir de entonces `fantasy_points` y `biwenger_prices` se publican con ambos.

### CLI

```
lfdata ingest biwenger --competition la-liga --season 2026        # temporada actual
lfdata ingest biwenger --competition segunda-division --season 2026
lfdata backfill biwenger --competition la-liga --from-season 2022 # históricas, con espera larga
```

## Orden de trabajo

1. `pyproject.toml`, estructura `src/lfdata`, CLI vacío, CI de GitHub Actions (lint + tests).
2. `storage` con backend local + tests.
3. `sources.http` + tests (falsificando el transporte, sin red).
4. Cliente Biwenger contra fixtures reales guardadas del experimento; después, ingesta real de la temporada actual.
5. Backend S3 y primera escritura al bucket real.
6. Backfill de 5 temporadas de La Liga y Segunda (lento a propósito: ~630 jugadores × temporada, 2 s entre peticiones ≈ 20 min por temporada y competición).

## Hecho cuando

- `lfdata ingest biwenger` deja la temporada actual completa en `raw/` y `curated/`, en local y en S3.
- Las 5 temporadas históricas de La Liga y Segunda están en el bucket.
- Un test de contrato falla si Biwenger cambia la forma de la respuesta.
