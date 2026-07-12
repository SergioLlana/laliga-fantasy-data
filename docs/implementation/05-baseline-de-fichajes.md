# Paso 5 — Baseline de fichajes

**Objetivo:** cuando un jugador sin historial en La Liga aparece en Biwenger, la plataforma le asigna una proyección inicial razonable en vez de dejarlo a cero o a ciegas.

## Idea central

Un fichaje trae señal de tres sitios: sus estadísticas por partido en la liga de origen (SofaScore, bajo demanda — paso 3), su valor de mercado en Transfermarkt (paso 2), y el precio que le pone el propio Biwenger. El baseline convierte esa señal en "puntos esperados por partido en La Liga" aplicando un ajuste por nivel de liga.

## Ajuste por nivel de liga

- Se estima con los **traslados históricos**: jugadores de las 5 temporadas de backfill que cambiaron de liga, comparando su rendimiento por 90 minutos antes y después del salto. El destino es siempre La Liga, así que los coeficientes son por liga de origen (Portugal→La Liga, Championship→La Liga...).
- Complemento decidido el 2026-07-12: una covariable continua de **nivel de liga = valor de plantilla Transfermarkt promedio de los equipos de la liga de origen** (de las páginas de competición de TM, ver paso 4 "datos nuevos"). Cubre las ligas con pocos o ningún traslado observado.
- Para ligas de origen con pocos traslados, el coeficiente se encoge hacia el de un grupo de ligas de nivel similar (la iteración 1 usa medias ponderadas por número de traslados). **Exploración prevista para la iteración 2**: modelo bayesiano jerárquico en Stan con priors por tier de competición — los coeficientes por liga cuelgan de su tier, y el partial pooling gestiona la escasez de forma natural.
- El experimento Forés marca el caso de prueba: Segunda → La Liga con datos de Biwenger en ambas, que sirve para verificar el método contra la verdad conocida.

## Integración con los modelos del paso 4

- El baseline no es un modelo aparte que compite con el de rendimiento: es el **prior del jugador nuevo**. En la práctica de la iteración 1: sus features de forma se rellenan con sus métricas de la liga de origen multiplicadas por el coeficiente del par de ligas, y el efecto jugador arranca en el valor que predice una regresión auxiliar (valor Transfermarkt + edad + posición → efecto jugador de los ya conocidos).
- El modelo de minutos para fichajes usa: precio Biwenger relativo al de su posición en su equipo, edad, y si el traspaso fue caro (los fichajes caros juegan).
- Según acumula partidos en La Liga, el peso del baseline decae de forma natural (más datos propios, menos prior).

## Casos que deben funcionar (tests de aceptación)

1. **Fichaje top-5**: delantero de la Premier con 3 temporadas de datos → baseline desde sus métricas ajustadas.
2. **Fichaje bajo demanda**: lateral del Brasileirão → el pipeline descarga su historial de SofaScore solo, lo mapea (o lo encola a revisión) y produce baseline.
3. **Ascendido de Segunda**: caso Forés → baseline directamente desde sus puntos Biwenger de Segunda con el coeficiente Segunda→Primera.
4. **Sin datos**: juvenil sin historial en ninguna fuente → baseline mínimo por posición + precio Biwenger, marcado como "confianza baja" (la web debe poder distinguirlo).

## Detector de jugador nuevo (hecho, 2026-07-12)

`lfdata newcomers --competition la-liga --season 2026` (módulo `lfdata.newcomers`, issue #19). Un **fichaje** es quien aparece en la plantilla de Biwenger sin puntos en ninguna temporada **anterior**, en ninguna de las dos competiciones: quien llega de Segunda no cuenta —sus puntos de Biwenger ya están curados y de ahí sale su baseline (caso Forés)—, y los puntos que un fichaje lleve ya en la temporada en curso no le quitan la condición, porque lo que le falta es historial del que proyectar.

Por cada fichaje detectado, sin intervención humana:

1. **Identidad** — refresca la plantilla de Transfermarkt de su **club de llegada** (`ingest_clubs`: una plantilla, no las veinte de la competición) y ejecuta el matcher. Sale con `canonical_id` o encolado en `mappings/players-review.csv` si el par es dudoso.
2. **Historial** — descarga su historial completo de SofaScore (`ingest_player`, bajo demanda, cualquier liga).

El registro va a la tabla curada `newcomers` (grano jugador-temporada de debut), que hace de marca de idempotencia: un fichaje con su historial `descargado` no vuelve a pedir nada. Los que quedaron en `sin-historial` (SofaScore no tiene ficha suya) o `fallo` (HTTP) se reintentan en el run siguiente; ninguno de los dos casos tira el pipeline.

**El detector vale lo que valga el histórico**. La condición es "sin puntos en temporadas anteriores", así que depende de que `fantasy_points` esté completo. Con el backfill de Biwenger a medias (#6), el dry-run del 2026-07-12 sobre las dos únicas temporadas curadas (2023 y 2024, parciales) marcó **271 fichajes de una plantilla de 635**: correcto según el dato, absurdo según la realidad. De ahí `--max-newcomers`: un fichaje son decenas de peticiones a dos fuentes, y un run anómalo —backfill incompleto, o una ingesta de Biwenger que falla y deja la tabla corta— no puede convertirse en cientos de peticiones a SofaScore. Hasta que #6 esté hecho, el detector se ejecuta con tope o en `--dry-run`.

**Eslabón que sigue siendo manual**: el historial que baja SofaScore llega a `player_match_stats` con su `sofascore_player_id` pero **sin `canonical_id`**, y el jugador queda encolado en `mappings/sofascore-review.csv`. El matcher automático solo cubre Biwenger↔Transfermarkt; el de SofaScore es la ronda manual del paso 3. Hasta que esa ronda se pase (o se automatice con nombre + fecha de nacimiento, como el de Transfermarkt), el historial de un fichaje está curado pero no se puede cruzar con su identidad, que es lo que el baseline necesita.

## Orden de trabajo

1. ~~Detector de "jugador nuevo" en la ingesta diaria de Biwenger (aparece en plantilla sin `fantasy_points` histórico) → dispara la descarga bajo demanda de SofaScore **y Transfermarkt**.~~ Hecho (#19), ver arriba.
2. Registro retrospectivo de **debutantes** por temporada del backfill (tabla curada jugador-temporada de debut) + descarga bajo demanda de su historial en la liga de origen, sea cual sea la liga — los traslados del punto 3 no existen sin esto para fichajes llegados de fuera de Segunda y las 5 grandes.
3. Tabla de traslados históricos entre ligas y estimación de coeficientes.
4. Relleno de features/prior para jugadores nuevos + los 4 casos de aceptación.
5. Backtest: fichajes reales de las temporadas 2023-24 a 2025-26, comparando el baseline contra sus puntos reales del primer tercio de temporada, y contra la alternativa ingenua (media de su posición).

## Hecho cuando

- Los 4 casos de aceptación pasan de principio a fin.
- El backtest muestra que el baseline predice el primer tercio de temporada de los fichajes mejor que la media por posición.
