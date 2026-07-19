# Runbook: backfill e incremental

Cómo completar la base de datos y mantenerla al día. Todos los comandos son
idempotentes y reanudables, así que repetir uno nunca rompe nada. Aquí solo
aparecen los flags operativos; el resto, en `lfdata <comando> --help`.

## Preparación

```bash
export AWS_PROFILE=lfdata
export LFDATA_DATA=s3://lfdata-data-593760774245
```

Sin esto los comandos escriben en `./data` local, que está incompleto.

## Orquestador (vía principal)

`lfdata run` encadena en orden los pasos de este runbook (issue #99) — la vía
principal para lanzar un backfill o un ciclo de incremental. Es la lógica que la
tarea Fargate del pipeline programado ([#24](https://github.com/SergioLlana/laliga-fantasy-data/issues/24))
invocará; hasta que exista, se lanza a mano o desde cron.

```bash
uv run lfdata run backfill --season N          # los diez pasos de la sección siguiente
uv run lfdata run incremental --season 2026 --cycle jornada   # bloque "tras cada jornada"
uv run lfdata run incremental --season 2026 --cycle semanal   # bloque "semanal"
```

Cada sub-paso ya es idempotente y reanudable por separado (`--resume`,
`--since-days`, skip de partidos ya en `raw/`): el orquestador no lleva checkpoint
propio, solo se **detiene limpio en el primer paso con fallos** (429/404 saltados,
un partido que no bajó) y deja un resumen de qué se completó y qué quedó
pendiente, con código de salida `!= 0`. Los pasos siguientes no se ejecutan porque
suelen depender del anterior (`map` necesita las plantillas ya curadas). Relanzar
el mismo comando retoma sin re-descargar lo ya bajado ni duplicar en curated,
apoyado en la reanudabilidad de cada sub-paso — típicamente tras esperar a que se
reponga la cuota de Biwenger (~30 min, ver más abajo).

Las secciones siguientes describen esos mismos pasos como comandos sueltos: sirven
de **fallback** para lanzar uno a mano (una prueba parcial con `--max-clubs`, un
paso que quieres repetir en aislado) y son la referencia de qué hace cada uno.

## Backfill (temporadas 2021–2025)

La temporada actual es 2026 y no se backfillea: entra por el incremental. Solo se
backfillea **La Liga**: Segunda y el resto de ligas de origen se cubren bajo demanda
cuando llega un fichaje (ver las secciones finales); Segunda admite además un backfill
**opcional** (última sección). Las temporadas se cargan
**temporada a temporada**, porque `map --season N` necesita las plantillas de
Biwenger y Transfermarkt de N ya curadas, y el matching de SofaScore necesita a su
vez el eventing de N ya descargado (de sus alineaciones sale el catálogo de identidad).

Equivale a `lfdata run backfill --season N` (fallback suelto). Para cada
temporada `N` de 2021 a 2025, en este orden:

```bash
# 1. Plantilla + reports de Biwenger (puntos, precios, minutos por jugador)
uv run lfdata ingest biwenger --competition la-liga --season N --resume
#    --resume        salta a los jugadores con report ya en raw/ (la temporada
#                    pasada es inmutable: reanudar sin re-descargar)
#    Nota: los precios NO tienen histórico (la API solo sirve los últimos ~366
#    días, #89): el backfill de una temporada pasada cura puntos pero 0 precios.

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

# 9. Copa del Rey y competiciones UEFA de esa temporada: densidad de calendario y
#    minutos entre semana de los equipos de La Liga (baja + cura fixtures/cup_minutes).
#    Va después del catálogo (paso 6): sabe quiénes son "los nuestros" por sofascore_teams
uv run lfdata backfill sofascore-cups --competition copa-del-rey --season N
uv run lfdata backfill sofascore-cups --competition champions-league --season N
uv run lfdata backfill sofascore-cups --competition europa-league --season N
uv run lfdata backfill sofascore-cups --competition conference-league --season N

# 10. Valor de plantilla por club de las 7 ligas cubiertas (una petición por liga):
#     nivel de equipo y de liga. Va tras el map (paso 7) para resolver el canónico de
#     los clubes de La Liga/Segunda; los extranjeros conservan su id de Transfermarkt
uv run lfdata ingest transfermarkt-values --season N
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

Equivale a `lfdata run incremental --season 2026 --cycle jornada` (fallback suelto):

```bash
uv run lfdata ingest biwenger --competition la-liga --season 2026 --delta
#    --delta         refresca solo a quienes puntuaron en las jornadas nuevas,
#                    en vez de recorrer la plantilla entera. Solo fantasy_points:
#                    ya no cura biwenger_prices (ADR 0012), ver "Diario"
uv run lfdata ingest biwenger-rounds --competition la-liga --season 2026 --resume
uv run lfdata backfill sofascore --competition la-liga --season 2026
uv run lfdata curate sofascore-catalog   # refresca el catálogo con las alineaciones nuevas
# Copa/UEFA de la jornada entre semana (fixtures + minutos de los equipos de La Liga)
uv run lfdata backfill sofascore-cups --competition copa-del-rey --season 2026
uv run lfdata backfill sofascore-cups --competition champions-league --season 2026
uv run lfdata backfill sofascore-cups --competition europa-league --season 2026
uv run lfdata backfill sofascore-cups --competition conference-league --season 2026
```

### Diario

El Precio es una señal de la plantilla entera, no solo de quien puntuó, así que se
mantiene aparte del delta de jornada ([ADR 0012](adr/0012-el-precio-no-va-por-el-delta.md)):

```bash
uv run lfdata ingest biwenger-prices --competition la-liga
#    1 petición: añade el precio de hoy de toda la plantilla a biwenger_prices
#    (upsert por player_id+fecha, temporada derivada de la fecha)
```

Será un paso del pipeline diario ([#24](https://github.com/SergioLlana/laliga-fantasy-data/issues/24))
en cuanto exista; hasta entonces se lanza a mano cada día.

### Periódico (red de seguridad de precios, p. ej. mensual)

El snapshot diario solo captura el precio del día en que corre: si algún día no se
lanza, el barrido completo del detalle por jugador rellena el hueco (dentro de la
ventana móvil de ~366 días que sirve la fuente), reutilizando el mismo comando del
backfill, sin `--delta` ([ADR 0012](adr/0012-el-precio-no-va-por-el-delta.md)):

```bash
uv run lfdata ingest biwenger --competition la-liga --season 2026
#    barrido completo (~634 peticiones, varias ventanas de cuota): red de
#    seguridad, no la vía principal de mantenimiento del precio
```

### Semanal

Equivale a `lfdata run incremental --season 2026 --cycle semanal` (fallback suelto):

```bash
uv run lfdata ingest transfermarkt --competition la-liga --season 2026 --since-days 7
uv run lfdata curate sofascore-catalog     # refresca el catálogo con las alineaciones ya descargadas
uv run lfdata newcomers --competition la-liga --season 2026
#    --dry-run        solo lista los fichajes detectados, sin descargar
#    --max-newcomers  tope de fichajes resueltos por run (útil con histórico a medias)
#    newcomers ejecuta `map` internamente (identidad antes de descargar, #81); con
#    Transfermarkt y el catálogo ya frescos, esta es la única pasada de mapping de
#    la semana — revisa mappings/*-review.csv (Transfermarkt y SofaScore) al terminar
uv run lfdata curate sofascore-canonical   # re-estampa el eventing con los mappings nuevos
uv run lfdata ingest transfermarkt-values --season 2026   # refresca el valor de plantilla (nivel de equipo/liga)
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

Segunda y el resto de ligas de origen **no se backfillean por defecto**: el historial
de un jugador de fuera de La Liga se descarga solo cuando llega. (Segunda admite además
un backfill **opcional** —ver la sección siguiente—, que no sustituye a este mecanismo:
es evidencia adicional, no una vía de resolución de fichajes.) El mecanismo habitual es
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

## Backfill opcional de Segunda (evidencia de matching y nivel de liga)

Segunda **no es obligatoria**: sus fichajes entran solos por `newcomers`, que los
enlaza e ingiere por jugador. Pero curar sus plantillas de Transfermarkt temporada a
temporada aporta dos cosas que el bajo demanda no da por adelantado: **evidencia de
matching de futuros ascendidos** —el matcher los cuadra solos por fecha en cuanto están
curados, antes de que asciendan— y **granularidad del nivel de liga** del baseline de
fichajes ([docs/implementation/05](implementation/05-baseline-de-fichajes.md)). Es la
Fase 3 (issue #93); hazlo cuando el baseline lo pida —no bloquea nada—.

Es **solo** la ingesta de Transfermarkt: ni Biwenger, ni SofaScore, ni `map` (el
matching de los ascendidos llega solo en las pasadas de `map` habituales, en cuanto sus
plantillas están curadas). Para cada temporada `N`:

```bash
uv run lfdata ingest transfermarkt --competition segunda-division --season N --since-days 30
#    --since-days N  no re-pide a la fuente al jugador bajado hace < N días,
#                    pero lo cura igual desde raw/ (reanudar el backfill)
```

Coste: ~22 clubes × ~28 jugadores × 5 peticiones a 4 s ≈ 3.000–3.500 peticiones ≈
~4 h/temporada, reanudable. La invariante de partición única
([ADR 0013](adr/0013-historial-transfermarkt-carrera-completa-particion-por-procedencia.md))
evita duplicar el historial de un jugador alcanzado también desde `la-liga` o
`bajo-demanda`: al escribirlo en `segunda-division` se retira de las demás particiones.
