# Paso 4 — Modelo de minutos y modelo de rendimiento

**Objetivo:** proyecciones de puntos por jornada para todos los jugadores de La Liga, en los cinco sistemas de puntuación, con validación temporal honesta.

## Decisiones ya tomadas que aplican aquí

- Dos modelos separados: minutos esperados × puntos condicionados a jugar.
- Enfoque en dos iteraciones: **primero GLM jerárquico** con librería estándar, **después Stan** (cmdstanpy), reutilizando la experiencia del motor bayesiano de world-cup-predictor.

## Iteración 0 — referencia a batir

"Los puntos de la próxima jornada = media de los últimos 5 partidos jugados" (y minutos igual). Sin esto no hay forma de saber si los modelos aportan.

## Iteración 1 — GLM jerárquico (statsmodels)

### Modelo de minutos

- Variable respuesta: minutos del jugador en el partido (0-90+), dominada por la decisión de titularidad.
- Estructura en dos partes: probabilidad de jugar ≥1 minuto (logística) × minutos esperados si juega (lineal). Efectos aleatorios por jugador (hábito de titularidad) y fijos por posición, racha de titularidades recientes, estado (lesión/sanción según Biwenger `status`), densidad de calendario.
- statsmodels: `BinomialBayesMixedGLM` para la parte logística y `MixedLM` para la parte continua.

### Modelo de rendimiento

- Variable respuesta: puntos del sistema de puntuación por partido, condicionado a haber jugado. Un ajuste por sistema (5 ajustes con las mismas features).
- Efectos: jugador (aleatorio), posición, calidad del rival (media de puntos concedidos por el rival a esa posición), local/visitante, forma reciente (media móvil de nota SofaScore y de métricas de `player_match_stats`), minutos esperados como offset.
- statsmodels: `MixedLM` sobre puntos por partido (los puntos fantasy son suficientemente continuos; los sistemas basados en nota son casi gaussianos).

### Validación

- Partición temporal estricta: entrenar hasta la jornada J, predecir J+1, rodando sobre las 2 últimas temporadas.
- Métricas: error absoluto medio de puntos por jornada frente a la referencia; para minutos, además, acierto de titularidad (clasificación).
- Informe reproducible: `lfdata evaluate --season 2025` escribe una tabla de métricas por posición y sistema de puntuación.

## Iteración 2 — Stan (cmdstanpy)

Mismo diseño generativo, ganando: incertidumbre completa por proyección (intervalos que la web puede enseñar), partial pooling bien controlado para jugadores con pocos partidos (clave para el paso 5), y priors explícitos. Se aborda siguiendo la skill `bayesian-workflow`, solo cuando la iteración 1 esté en producción y medida.

## Registro de modelos (reentrenamiento, versionado y evaluación continua)

Los modelos irán cambiando de features y de preprocesado, así que **una versión de modelo sin la versión exacta de sus datos no reproduce nada**. El registro versiona ambas cosas juntas. Es un registro propio sobre S3 (a esta escala, un índice JSON y prefijos versionados dan lo mismo que MLflow sin añadir infraestructura):

```
s3://lfdata-data-*/models/
├── registry.json                # índice: versiones, estado (candidate|active|archived) y puntero al activo
└── {version}/                   # p. ej. 2026-08-14-a
    ├── artifacts/               # modelos serializados: minutos + rendimiento × 5 sistemas
    ├── feature_spec.json        # lista de features y parámetros de preprocesado usados
    ├── training_data.parquet    # la tabla de entrenamiento exacta (unos MB; reproducibilidad total)
    ├── metrics.json             # informe de la validación temporal
    └── MANIFEST.json            # git sha del código, particiones de curated leídas, fechas, quién entrenó
```

Reglas:

- **Reentrenamiento semanal** (no diario): los efectos por jugador cambian despacio; reentrenar a diario solo añade ruido y coste.
- **Promoción siempre manual**: cada entrenamiento entra como `candidate`; solo `lfdata models activate {version}` lo convierte en `active` (el que alimenta la web). El informe de comparación contra el activo se genera solo, pero la decisión es humana. Salvaguarda contra el olvido: si el activo lleva más de N semanas sin renovarse habiendo candidatos mejores, el pipeline avisa.
- **Proyecciones versionadas**: `projections` está particionada por versión de modelo. El pipeline diario proyecta con el activo **y con todos los candidatos vivos** ("proyección en la sombra"), de modo que `projection_accuracy` registra el acierto real de cada versión en jornadas nuevas — la promoción se decide con acierto en producción, no solo con validación retrospectiva. La web enseña únicamente la partición del activo.
- **Vuelta atrás**: reactivar la versión anterior es cambiar el puntero en `registry.json`; sus artefactos, datos, features y proyecciones siguen intactos en su prefijo/partición.
- **Evaluación continua**: tras cada jornada, el pipeline compara las proyecciones publicadas de **todas** las versiones vivas contra los puntos reales y lo añade a `projection_accuracy` (grano jornada-sistema-posición-versión). Sirve de monitorización, de base para promocionar, y de credibilidad pública: la web puede enseñar el acierto histórico del modelo activo.
- **Ciclo de vida**: un candidato que no se promociona se archiva manualmente (`lfdata models archive`) para que la sombra no crezca sin límite; lo archivado conserva todo pero deja de proyectar.

El comando `lfdata train` escribe la versión nueva completa; `lfdata models list|activate|archive {version}` gestiona el índice.

## Tabla curada que produce

`projections` — grano jugador-jornada-sistema-**versión de modelo** (particionada por versión): canonical_id, jornada, sistema de puntuación, minutos esperados, puntos esperados, intervalo (p10/p90 en iteración 2), fecha de cálculo. Las proyecciones de temporada completa son la suma sobre jornadas restantes. La web lee solo la partición de la versión activa.

## Orden de trabajo

1. `lfdata.features`: construcción de la tabla de entrenamiento desde `fantasy_points` + `player_match_stats` + calendario (tests con casos fijados).
2. Referencia (iteración 0) + arnés de validación temporal. **El arnés va antes que cualquier modelo.**
3. Modelo de minutos, evaluar; modelo de rendimiento, evaluar.
4. Composición y escritura de `projections`; comando `lfdata project --round J`.
5. (Después, sin prisa) Iteración 2 en Stan.

## Hecho cuando

- El pipeline produce `projections` para la próxima jornada en un solo comando.
- Ambos modelos baten a la referencia en la validación rodante de 2 temporadas, por posición.
- El informe de evaluación queda guardado y versionado con cada versión de modelo.
