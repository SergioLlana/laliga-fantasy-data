# Paso 4 — Modelo de minutos y modelo de rendimiento

**Objetivo:** proyecciones de puntos por jornada para todos los jugadores de La Liga, en los cinco sistemas de puntuación, con validación temporal honesta.

## Decisiones ya tomadas que aplican aquí

- Dos modelos separados: minutos esperados × puntos condicionados a jugar.
- Enfoque en dos iteraciones: **primero GLM jerárquico** con librería estándar, **después Stan** (cmdstanpy), reutilizando la experiencia del motor bayesiano de world-cup-predictor.

## Iteración 0 — referencia a batir

"Los puntos de la próxima jornada = media de los últimos 5 partidos jugados" (y minutos igual). Sin esto no hay forma de saber si los modelos aportan.

## Análisis previo — eventos → puntos por posición (decidido 2026-07-12)

Antes de fijar las features del modelo de rendimiento, un análisis descriptivo sobre el histórico: regresión `puntos ~ estadísticas de eventos` por **posición** y **sistema de puntuación** (los cinco, mismo método). Objetivos:

- Para el sistema Estadísticas (regla casi determinista sobre eventos) debe salir un ajuste casi perfecto — si no, no entendemos el dato.
- Para los sistemas de nota (SofaScore, Picas/AS, Media) revela qué eventos mueven la nota en cada posición (¿pesan los despejes de un lateral? ¿las paradas?).
- El resultado es la **shortlist de features de forma** del modelo de rendimiento, con evidencia documentada, y alimenta el principio de "confianza mostrando el porqué" de PRODUCT.md.

Se documenta como experimento en `docs/experiments/`. No cambia la arquitectura: el modelo de rendimiento sigue prediciendo puntos directamente (se descartó el modelo en dos etapas eventos→puntos por multiplicar los modelos a mantener).

## Iteración 1 — GLM jerárquico (statsmodels)

### Modelo de minutos

- Variable respuesta: minutos del jugador en el partido (0-90+), dominada por la decisión de titularidad.
- Estructura en dos partes: probabilidad de jugar ≥1 minuto (logística) × minutos esperados si juega (lineal). **Vía de mejora decidida con datos** (2026-07-12): si el error de minutos lo justifica, pasar a tres estados (titular / entra desde el banquillo / no juega) × minutos condicionados al estado — el dato de titularidad ya está ingerido (SofaScore `substitute`, Transfermarkt minuto de entrada/salida).
- Features:
  - Efecto aleatorio por jugador (hábito de titularidad); posición (fijo).
  - Racha de titularidades recientes (no solo "jugó": distinguir titular de suplente).
  - Estado actual (lesión/sanción según Biwenger `status`).
  - Lesiones desde el historial de Transfermarkt: **partidos desde la vuelta de lesión** (dosificación post-lesión que el `status` ya no refleja) y **días lesionado en los últimos 12 meses** (fragilidad).
  - Edad, como spline (no lineal).
  - Densidad de calendario **incluyendo Copa y competiciones UEFA**: días desde el último partido de cualquier competición, partido entre semana próximo, y minutos jugados en esos partidos (requiere ingerir fixtures + alineaciones de esas competiciones para equipos de La Liga; los rivales extranjeros no necesitan mapping de jugadores).
- statsmodels: `BinomialBayesMixedGLM` para la parte logística y `MixedLM` para la parte continua.

### Modelo de rendimiento

- Variable respuesta: puntos del sistema de puntuación por partido, condicionado a haber jugado. Un ajuste por sistema (5 ajustes con las mismas features).
- Features:
  - Efecto aleatorio por jugador; posición (fijo); local/visitante.
  - **Nivel de equipo, propio y rival: valor de plantilla de Transfermarkt** (decidido 2026-07-12). Se descartó de momento la "media de puntos fantasy concedidos por el rival a cada posición"; queda anotada como mejora futura (captura matchups específicos que el valor de plantilla no ve).
  - Forma reciente: media móvil de nota SofaScore y de las métricas de `player_match_stats` que el análisis eventos→puntos señale como relevantes por posición.
  - Edad, como spline (no lineal).
  - Minutos esperados como offset.
- **Excluido a propósito** (2026-07-12): el precio de Biwenger (nivel y variación). Captura sabiduría de la multitud (noticias, alineaciones probables) pero acoplaría los modelos con la recomendación de infravalorados (proyección vs. precio sería circular). Queda como vía de exploración futura; si se incorpora, hay que excluirlo de esa comparativa.
- statsmodels: `MixedLM` sobre puntos por partido (los puntos fantasy son suficientemente continuos; los sistemas basados en nota son casi gaussianos).

### Datos nuevos que requieren ingesta

1. **Fixtures + alineaciones de Copa del Rey y competiciones UEFA** (equipos de La Liga) vía SofaScore, para la densidad de calendario y los minutos entre semana.
2. **Página de competición de Transfermarkt** (valor de plantilla por club) para las ligas cubiertas: una petición por liga-temporada, refresco al cierre de cada ventana de mercado. Da el nivel de equipo (propio/rival/extranjero) y, promediando clubes, el nivel de liga para el paso 5 — sin necesitar mappings de jugadores extranjeros.

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

1. Análisis eventos→puntos por posición y sistema (experimento documentado) → shortlist de features de forma.
2. `lfdata.features`: construcción de la tabla de entrenamiento desde `fantasy_points` + `player_match_stats` + calendario (tests con casos fijados).
3. Referencia (iteración 0) + arnés de validación temporal. **El arnés va antes que cualquier modelo.**
4. Modelo de minutos, evaluar; modelo de rendimiento, evaluar.
5. Composición y escritura de `projections`; comando `lfdata project --round J`.
6. (Después, sin prisa) Iteración 2 en Stan.

## Hecho cuando

- El pipeline produce `projections` para la próxima jornada en un solo comando.
- Ambos modelos baten a la referencia en la validación rodante de 2 temporadas, por posición.
- El informe de evaluación queda guardado y versionado con cada versión de modelo.
