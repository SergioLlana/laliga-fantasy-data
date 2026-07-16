# Guía para agentes

Plataforma abierta de datos y proyecciones para fantasy de La Liga (Biwenger primero). Este fichero es el punto de entrada; lee lo que enlaza antes de tocar código.

## Por dónde empezar

- **[CONTEXT.md](CONTEXT.md)** — el lenguaje del dominio (fichaje, jugador canónico, mapping, sistema de puntuación…). Usa estos términos; evita los sinónimos que lista.
- **[PRODUCT.md](PRODUCT.md)** y **[docs/plan.md](docs/plan.md)** — qué ofrece el producto y el plan por fases.
- **[docs/adr/](docs/adr/)** — las decisiones estructurales y su *porqué*. Un ADR que contradice lo que vas a hacer es una señal de que falta contexto: léelo antes.
- **[README.md](README.md)** — instalación y todos los comandos del CLI con ejemplos.
- **[docs/runbook.md](docs/runbook.md)** — cómo operar la base de datos: backfill de temporadas pasadas e incremental de la temporada en curso, con orden y cadencias.

## Convenciones que no se deducen del código

- **`--season` es el año de inicio** en todas las fuentes: `2025` = temporada 2025/26. La traducción a la numeración de cada API vive en su cliente.
- **Los datos viven en S3, no en local.** El bucket es `s3://lfdata-data-593760774245` (perfil `lfdata`). Para trabajar contra él: `export AWS_PROFILE=lfdata` y pasa `--data s3://…` o `export LFDATA_DATA=s3://…`. El `data/` local está en `.gitignore` y suele estar incompleto.
- **Dos capas de almacenamiento** ([ADR 0003](docs/adr/0003-s3-raw-plus-curated-layers.md)): `raw/` es la respuesta cruda tal cual (única fuente de verdad reprocesable) y `curated/` son las tablas Parquet. **La capa curada se reconstruye siempre desde `raw/`**; lo que `--since-days`/`--resume` evitan es la *descarga*, nunca el *curado*.
- **De Biwenger solo se ingiere `la-liga`** ([ADR 0008](docs/adr/0008-de-biwenger-solo-se-ingiere-la-liga.md)): sus ids de jugador y equipo cambian por competición. Segunda y Copa se cubren con Transfermarkt/SofaScore como ligas de origen.
- **Cuota de Biwenger**: ~200 peticiones por ventana de ~30 min e IP. Al agotarla, el transporte desborda a ScrapeOps ([ADR 0004](docs/adr/0004-scrapeops-como-desbordamiento.md)), que gasta créditos. Los backfills largos son reanudables (`--resume`).

## Mappings (identidad canónica)

Antes de tocar `src/lfdata/mappings/` o `mappings/*.csv`, lee **[mappings/README.md](mappings/README.md)** y estos tres ADR, que se contradicen con la intuición ingenua:

- **[ADR 0001](docs/adr/0001-canonical-player-ids-with-manual-review.md)** — IDs canónicos propios; un canónico tiene como máximo un mapping por fuente. Es la decisión más cara de revertir del proyecto.
- **[ADR 0006](docs/adr/0006-la-identidad-no-tiene-temporada.md)** — la identidad no tiene temporada: `--season` decide de qué plantillas salen los clubes, no a quién se puede mapear. El club es una pista, no un filtro; la fecha de nacimiento gradúa la confianza.
- **[ADR 0007](docs/adr/0007-los-entrenadores-no-son-jugadores.md)** — los entrenadores no entran en `biwenger_players`.

Los mappings viven en git (no en S3): el trabajo de revisión manual se versiona y se aprueba en pull request. `mappings/*-review.csv` son los dudosos; nunca se marca `skip` a la ligera, porque un `skip` se aprueba para siempre y no se vuelve a proponer.

## Desarrollo

```bash
uv run pytest              # tests
uv run ruff check .        # lint
uv run ruff format .       # formateo
```

CI corre `pytest` y `ruff` (no `lfdata map --check`: en CI no hay datos curados). Los tests montan tablas curadas sintéticas pequeñas, sin red.

## Pendientes vivos

`todo.md` recoge lo que está a medias o decidido pero sin implementar. Míralo antes de arrancar algo nuevo.
