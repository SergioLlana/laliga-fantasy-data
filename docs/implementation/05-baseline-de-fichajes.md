# Paso 5 — Baseline de fichajes

**Objetivo:** cuando un jugador sin historial en La Liga aparece en Biwenger, la plataforma le asigna una proyección inicial razonable en vez de dejarlo a cero o a ciegas.

## Idea central

Un fichaje trae señal de tres sitios: sus estadísticas por partido en la liga de origen (SofaScore, bajo demanda — paso 3), su valor de mercado en Transfermarkt (paso 2), y el precio que le pone el propio Biwenger. El baseline convierte esa señal en "puntos esperados por partido en La Liga" aplicando un ajuste por nivel de liga.

## Ajuste por nivel de liga

- Se estima con los **traslados históricos**: jugadores de las 5 temporadas de backfill que cambiaron de liga, comparando su rendimiento por 90 minutos antes y después del salto. El destino es siempre La Liga, así que los coeficientes son por liga de origen (Portugal→La Liga, Championship→La Liga...).
- Complemento decidido el 2026-07-12: una covariable continua de **nivel de liga = valor de plantilla Transfermarkt promedio de los equipos de la liga de origen** (de las páginas de competición de TM, ver paso 4 "datos nuevos"). Cubre las ligas con pocos o ningún traslado observado.
- Para ligas de origen con pocos traslados, el coeficiente se encoge hacia el de un grupo de ligas de nivel similar (la iteración 1 usa medias ponderadas por número de traslados). **Exploración prevista para la iteración 2**: modelo bayesiano jerárquico en Stan con priors por tier de competición — los coeficientes por liga cuelgan de su tier, y el partial pooling gestiona la escasez de forma natural.
- **Segunda es una liga de origen más** (decidido el 2026-07-13): el ascendido se trata como cualquier otro fichaje —eventing de SofaScore y valor de Transfermarkt, corregidos por el coeficiente Segunda→Primera—, no con sus puntos de Biwenger en Segunda. Un método por competición de origen sería un caso especial que no escala, y el modelo no tendría cómo aprender el salto de Segunda si esa liga se saltara el mecanismo común.
- Que en Segunda **sí** tengamos puntos de Biwenger es lo que la convierte en el banco de pruebas: es el único salto donde conocemos la verdad en ambos lados. El experimento Forés valida ahí el método (baseline predicho vs. puntos reales), sin que esos puntos alimenten el baseline.

## Integración con los modelos del paso 4

- El baseline no es un modelo aparte que compite con el de rendimiento: es el **prior del jugador nuevo**. En la práctica de la iteración 1: sus features de forma se rellenan con sus métricas de la liga de origen multiplicadas por el coeficiente del par de ligas, y el efecto jugador arranca en el valor que predice una regresión auxiliar (valor Transfermarkt + edad + posición → efecto jugador de los ya conocidos).
- El modelo de minutos para fichajes usa: precio Biwenger relativo al de su posición en su equipo, edad, y si el traspaso fue caro (los fichajes caros juegan).
- Según acumula partidos en La Liga, el peso del baseline decae de forma natural (más datos propios, menos prior).

## Casos que deben funcionar (tests de aceptación)

1. **Fichaje top-5**: delantero de la Premier con 3 temporadas de datos → baseline desde sus métricas ajustadas.
2. **Fichaje bajo demanda**: lateral del Brasileirão → el pipeline descarga su historial de SofaScore solo, lo mapea (o lo encola a revisión) y produce baseline.
3. **Ascendido de Segunda**: caso Forés → mismo camino que cualquier fichaje (eventing de SofaScore en Segunda + valor Transfermarkt, con el coeficiente Segunda→Primera). Sus puntos de Biwenger en Segunda no entran en el baseline: son la verdad contra la que se comprueba.
4. **Sin datos**: juvenil sin historial en ninguna fuente → baseline mínimo por posición + precio Biwenger, marcado como "confianza baja" (la web debe poder distinguirlo).

## Detector de jugador nuevo (hecho, 2026-07-12)

`lfdata newcomers --competition la-liga --season 2026` (módulo `lfdata.newcomers`, issue #19). Un **fichaje** es quien aparece en la plantilla de una competición de Biwenger sin puntos en ninguna temporada **anterior de esa misma competición**. El ascendido de Segunda lo es (no tiene puntos de La Liga, y Segunda es una liga de origen más), y los puntos que un fichaje lleve ya en la temporada en curso no le quitan la condición, porque lo que le falta es historial del que proyectar, no minutos.

Por cada fichaje detectado, sin intervención humana:

1. **Identidad** — refresca la plantilla de Transfermarkt de su **club de llegada** (`ingest_clubs`: una plantilla, no las veinte de la competición) y ejecuta el matcher. Sale con `canonical_id` o encolado en `mappings/players-review.csv` si el par es dudoso.
2. **Historial** — **primero identidad, después descarga** (issue #81): nunca «al primero que salga». Si el matcher ya colgó su id de SofaScore del canónico (jugó en liga backfilleada, está en el catálogo `sofascore_players`), se descarga por ID. Si no, se resuelve por `search/all` filtrado a fútbol y a nombre compatible: candidato único cuya **fecha de nacimiento** cuadra con la de Biwenger (una sola petición a la ficha `player/{id}` para traerla) → se aprueba el mapping y se descarga por ID; cero o varios candidatos, o fecha discrepante → se encola a `mappings/sofascore-review.csv` y se **aplaza**, sin curar nada. El mapping se aprueba **antes** de descargar, así que ninguna fila entra en `player_match_stats`/`player_season_stats` sin `canonical_id` verificado.

El registro va a la tabla curada `newcomers` (grano jugador-temporada de debut), que hace de marca de idempotencia: un fichaje con su historial `descargado` no vuelve a pedir nada. Los que quedaron en `sin-identidad` (identidad de SofaScore sin verificar: sin canónico todavía, o búsqueda ambigua/discrepante), `sin-historial` (SofaScore no tiene ficha de fútbol suya) o `fallo` (HTTP) se reintentan en el run siguiente; ninguno tira el pipeline. Que `descargado` implique identidad verificada es justo lo que evita fosilizar el historial de la persona equivocada bajo un `sofascore_player_id` que nunca se reintenta.

**El detector vale lo que valga el histórico**. La condición es "sin puntos en temporadas anteriores", así que depende de que `fantasy_points` esté completo. Con el backfill de Biwenger a medias (#6), el dry-run del 2026-07-12 sobre las dos únicas temporadas curadas (2023 y 2024, parciales) marcó **271 fichajes de una plantilla de 635**: correcto según el dato, absurdo según la realidad. De ahí `--max-newcomers`: un fichaje son decenas de peticiones a dos fuentes, y un run anómalo —backfill incompleto, o una ingesta de Biwenger que falla y deja la tabla corta— no puede convertirse en cientos de peticiones a SofaScore. Hasta que #6 esté hecho, el detector se ejecuta con tope o en `--dry-run`.

**Identidad ya resuelta antes de curar** (#74 + #81): el matcher de SofaScore se automatizó con nombre + fecha de nacimiento (como el de Transfermarkt), y el detector la verifica **antes** de descargar. Un fichaje que juega en liga backfilleada entra en el catálogo `sofascore_players` y el matcher lo cuelga solo; uno de una liga no cubierta se verifica por `search/all` + fecha con una única petición. En ambos casos el historial llega a `player_match_stats` ya con `canonical_id`; solo los casos dudosos quedan en `mappings/sofascore-review.csv` a la espera de una `decision` manual, con la descarga aplazada hasta entonces.

## Orden de trabajo

1. ~~Detector de "jugador nuevo" en la ingesta diaria de Biwenger (aparece en plantilla sin `fantasy_points` histórico) → dispara la descarga bajo demanda de SofaScore **y Transfermarkt**.~~ Hecho (#19), ver arriba.
2. Registro retrospectivo de **debutantes** por temporada del backfill (tabla curada jugador-temporada de debut) + descarga bajo demanda de su historial en la liga de origen, sea cual sea la liga — los traslados del punto 3 no existen sin esto para fichajes llegados de fuera de Segunda y las 5 grandes.
3. Tabla de traslados históricos entre ligas y estimación de coeficientes.
4. Relleno de features/prior para jugadores nuevos + los 4 casos de aceptación.
5. Backtest: fichajes reales de las temporadas 2023-24 a 2025-26, comparando el baseline contra sus puntos reales del primer tercio de temporada, y contra la alternativa ingenua (media de su posición).

## Hecho cuando

- Los 4 casos de aceptación pasan de principio a fin.
- El backtest muestra que el baseline predice el primer tercio de temporada de los fichajes mejor que la media por posición.
