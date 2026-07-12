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

## Orden de trabajo

1. Detector de "jugador nuevo" en la ingesta diaria de Biwenger (aparece en plantilla sin `fantasy_points` histórico) → dispara la descarga bajo demanda de SofaScore **y Transfermarkt**.
2. Registro retrospectivo de **debutantes** por temporada del backfill (tabla curada jugador-temporada de debut) + descarga bajo demanda de su historial en la liga de origen, sea cual sea la liga — los traslados del punto 3 no existen sin esto para fichajes llegados de fuera de Segunda y las 5 grandes.
3. Tabla de traslados históricos entre ligas y estimación de coeficientes.
4. Relleno de features/prior para jugadores nuevos + los 4 casos de aceptación.
5. Backtest: fichajes reales de las temporadas 2023-24 a 2025-26, comparando el baseline contra sus puntos reales del primer tercio de temporada, y contra la alternativa ingenua (media de su posición).

## Hecho cuando

- Los 4 casos de aceptación pasan de principio a fin.
- El backtest muestra que el baseline predice el primer tercio de temporada de los fichajes mejor que la media por posición.
