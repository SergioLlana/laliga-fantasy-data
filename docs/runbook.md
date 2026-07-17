# Runbook: backfill e incremental

Cómo completar la base de datos y mantenerla al día. Los comandos se lanzan a mano
(el pipeline programado es el paso 7 de [plan.md](plan.md)); todos son idempotentes
y reanudables, así que repetir uno nunca rompe nada. Aquí solo aparecen los flags
operativos; el resto, en `lfdata <comando> --help`.

## Preparación

```bash
export AWS_PROFILE=lfdata
export LFDATA_DATA=s3://lfdata-data-593760774245
```

Sin esto los comandos escriben en `./data` local, que está incompleto.

## Backfill (temporadas 2021–2025)

La temporada actual es 2026 y no se backfillea: entra por el incremental. Solo se
backfillea **La Liga**: Segunda y el resto de ligas de origen se cubren bajo demanda
cuando llega un fichaje (ver la última sección). Las temporadas se cargan
**temporada a temporada**, porque `map --season N` necesita las plantillas de
Biwenger y Transfermarkt de N ya curadas, y el matching de SofaScore necesita a su
vez el eventing de N ya descargado (de sus alineaciones sale el catálogo de identidad).

Para cada temporada `N` de 2021 a 2025, en este orden:

```bash
# 1. Plantilla + reports de Biwenger (puntos, precios, minutos por jugador)
uv run lfdata ingest biwenger --competition la-liga --season N --resume
#    --resume        salta a los jugadores con report ya en raw/ (la temporada
#                    pasada es inmutable: reanudar sin re-descargar)

# 2. Puntos por jornada de todos los jugadores (histórico sin sesgo de plantilla)
uv run lfdata ingest biwenger-rounds --competition la-liga --season N --resume
#    --resume        salta las jornadas ya curadas

# 3. Plantillas de Transfermarkt
uv run lfdata ingest transfermarkt --competition la-liga --season N --since-days 30
#    --since-days N  no re-pide a la fuente al jugador bajado hace < N días,
#                    pero lo cura igual desde raw/

# 4. Mappings Biwenger↔Transfermarkt (crea los IDs canónicos) y revisión de dudosos
uv run lfdata map --season N
#    revisar mappings/*-review.csv (ver mappings/README.md); nunca `skip` a la ligera

# 5. Eventing de SofaScore (reanudable de serie: solo descarga los partidos que faltan)
uv run lfdata backfill sofascore --competition la-liga --season N
#    --max-matches / --max-pages   acotan una prueba parcial

# 6. Catálogo de identidad de SofaScore desde raw/ (de las alineaciones del paso 5),
#    sin peticiones: es la evidencia que el matcher necesita para SofaScore
uv run lfdata curate sofascore-catalog

# 7. Segunda pasada de mappings: ahora cuelga SofaScore del canónico de cada jugador
uv run lfdata map --season N
#    revisar mappings/sofascore-review.csv (mismos criterios; ver mappings/README.md)

# 8. Re-estampa el canonical_id en el eventing ya curado (cruce con los mappings,
#    sin releer raw/), para que player_match_stats deje de estar huérfano
uv run lfdata curate sofascore-canonical
```

Al terminar las cinco temporadas:

```bash
uv run lfdata map --check   # falla si Biwenger o el eventing quedaron sin canonical_id
uv run lfdata crosscheck sofascore-biwenger-minutes --out crosscheck.json
```

**Cuota de Biwenger**: ~200 peticiones por ventana de ~30 min ([ADR 0004](adr/0004-scrapeops-como-desbordamiento.md));
los pasos 1–2 de una temporada no caben en una ventana. Relanza con `--resume`
hasta que el comando termine sin descargas nuevas.

**Re-cura del eventing sin re-descargar** (cuando cambia la *lógica* de curado, no
solo el `canonical_id`): el paso 8 (`sofascore-canonical`) rellena únicamente esa
columna de join. Si tocas cómo se construye una fila de `player_match_stats` (una
métrica nueva, un cambio en cómo se deriva un campo…), reconstruye la tabla entera
desde `raw/` sin pedir nada a la fuente:

```bash
uv run lfdata curate sofascore-matches   # relee event-lineups de raw/, rehace la fila entera
```

El backfill (paso 5) sigue saltando la descarga de lo que ya está en `raw/`; este
comando es el que además re-cura, cumpliendo la convención de [ADR 0003](adr/0003-s3-raw-plus-curated-layers.md)
(el curado se reconstruye siempre desde raw/).

## Incremental (temporada 2026, durante la temporada)

### Tras cada jornada

```bash
uv run lfdata ingest biwenger --competition la-liga --season 2026 --delta
#    --delta         refresca solo a quienes puntuaron en las jornadas nuevas,
#                    en vez de recorrer la plantilla entera
uv run lfdata ingest biwenger-rounds --competition la-liga --season 2026 --resume
uv run lfdata backfill sofascore --competition la-liga --season 2026
uv run lfdata curate sofascore-catalog   # refresca el catálogo con las alineaciones nuevas
```

### Semanal

```bash
uv run lfdata newcomers --competition la-liga --season 2026
#    --dry-run        solo lista los fichajes detectados, sin descargar
#    --max-newcomers  tope de fichajes resueltos por run (útil con histórico a medias)
uv run lfdata ingest transfermarkt --competition la-liga --season 2026 --since-days 7
uv run lfdata curate sofascore-catalog     # por si newcomers trajo alineaciones nuevas
uv run lfdata map --season 2026            # + revisar mappings/*-review.csv (Transfermarkt y SofaScore)
uv run lfdata curate sofascore-canonical   # re-estampa el eventing con los mappings nuevos
uv run lfdata map --check
```

### Cierre de temporada (julio)

Con el cambio de temporada (plan: "riesgos") toca actualizar los IDs de temporada,
ingerir las plantillas nuevas y pasar una ronda de mappings. Los ascendidos entran
solos: son fichajes como cualquier otro y el detector les descarga su historial.

```bash
uv run lfdata ingest biwenger --competition la-liga --season 2027
uv run lfdata ingest transfermarkt --competition la-liga --season 2027
uv run lfdata map --season 2027    # + revisar mappings/*-review.csv
uv run lfdata newcomers --competition la-liga --season 2027 --max-newcomers 20
```

## Fichajes de otras ligas (bajo demanda)

Segunda y el resto de ligas de origen **no se backfillean**: el historial de un
jugador de fuera de La Liga se descarga solo cuando llega. El mecanismo habitual es
el detector de fichajes del bloque semanal (`newcomers`): detecta a quien está en la
plantilla sin puntos en temporadas anteriores, refresca la plantilla de Transfermarkt
de su club de llegada, lo mapea (o lo encola a `mappings/*-review.csv`) y baja su
historial completo de SofaScore. Es idempotente: un fichaje resuelto no vuelve a
generar peticiones.

Para un jugador suelto (un rumor que quieres estudiar, un caso que `newcomers` no
resolvió) está la ingesta manual:

```bash
uv run lfdata ingest sofascore --player "Alex Fores"   # nombre, id de SofaScore
uv run lfdata ingest sofascore --player p00123         # o canonical_id ya mapeado
```
