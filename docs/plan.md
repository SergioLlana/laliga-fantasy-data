# Plan de desarrollo

Plataforma abierta para jugadores de fantasy de La Liga. Empieza soportando Biwenger; el código se organiza para poder añadir otras plataformas de fantasy más adelante.

Los términos del dominio están definidos en [CONTEXT.md](../CONTEXT.md) y las decisiones estructurales en [docs/adr/](./adr/).

## Qué ofrece el producto

1. **Proyección de puntos**: predicción de puntos por jornada y por temporada para cada jugador de La Liga, bajo los cinco sistemas de puntuación de Biwenger (Picas/AS, SofaScore, Media, Estadísticas y Biwenger Social).
2. **Baseline de fichajes**: para jugadores recién llegados de otras ligas, una proyección inicial estimada con su historial fuera de La Liga (nota SofaScore, métricas de partido, valor en Transfermarkt).
3. **Recomendaciones de mercado**: jugadores infravalorados o sobrevalorados comparando su precio en Biwenger con su proyección.
4. **Explorador de datos**: consulta de históricos de valores de mercado, puntos y fichajes.
5. **Alertas de cláusulas** y **optimización de alineación** (fase 2): requieren acceso a la liga privada del usuario.

## Fases

**Fase 1 — solo datos públicos.** Todo lo anterior salvo el punto 5. Nadie necesita conectar su cuenta de Biwenger. Esto cubre la mayor parte del valor y evita, de momento, el problema de manejar credenciales de terceros (Biwenger no tiene un mecanismo oficial para autorizar aplicaciones externas).

**Fase 2 — cuenta conectada.** Alertas de cláusulas y optimización de alineación sobre el equipo real del usuario. Antes de empezarla hay que decidir cómo obtiene el usuario su token de Biwenger de forma aceptable.

## Fuentes de datos

| Fuente | Qué aporta | Cómo se accede |
|---|---|---|
| Biwenger | Jugadores, precios diarios (histórico por temporada), puntos por partido en los cinco sistemas de puntuación, minutos jugados y nota SofaScore por partido, jornadas | API no oficial (JSON), verificada: acepta `season=YYYY` para históricos |
| SofaScore | Nota y estadísticas por jugador-partido (115 campos por temporada), en La Liga y el resto de ligas, incluidas categorías inferiores | API no oficial (JSON); requiere impersonación de Chrome (curl-cffi) |
| Transfermarkt | Valor de mercado, traspasos y cesiones, datos biográficos, disponibilidad por partido e historial de lesiones | Endpoints JSON internos (`ceapi`) para valores, traspasos y rendimiento/disponibilidad (`performance-game`); HTML para búsqueda, perfil y lesiones |
| FotMob (verificado 2026-07-07) | Redundancia de SofaScore: estadísticas por jugador-partido, con nota propia | API no oficial (JSON); requiere impersonación de Chrome |

Normas de descarga para todas las fuentes: una petición cada pocos segundos, reintentos con espera creciente, identificación de navegador realista, y toda respuesta se guarda antes de interpretarla. La capa de red está abstraída para poder enchufar un proveedor de proxies (ScrapeOps, empezando por su plan gratuito) si una fuente empieza a bloquearnos. Cada ingestor valida la estructura de la página o respuesta y falla con un error claro si la fuente cambió su formato.

## Datos en S3

Dos capas (ver ADR 0003):

```
s3://<bucket>/
├── raw/                        # respuestas tal cual llegan
│   └── {fuente}/{dataset}/fecha_descarga=YYYY-MM-DD/...
└── curated/                    # tablas limpias en Parquet, con IDs canónicos
    ├── players.parquet             # jugador canónico: nombre, nacimiento, posición
    ├── teams.parquet
    ├── player_mappings.parquet     # ID de cada fuente → ID canónico
    ├── team_mappings.parquet
    ├── player_match_stats.parquet  # una fila por jugador-partido (todas las ligas)
    ├── fantasy_points.parquet      # puntos por jugador-partido y sistema de puntuación
    ├── market_values.parquet       # precio diario en Biwenger + valor Transfermarkt
    ├── transfers.parquet
    ├── projections/                 # salida de los modelos, por jornada, particionada por versión de modelo
    └── projection_accuracy.parquet  # acierto real por jornada, sistema y versión de modelo
└── models/                     # registro de modelos: cada versión con sus artefactos,
                                # features, datos de entrenamiento y métricas (ver implementation/04)
```

Histórico inicial: 5 temporadas (desde 2021-22), en La Liga y en las ligas de origen de los fichajes.

## Identidad de jugadores y equipos

Cada fuente usa sus propios nombres e IDs. Mantenemos un ID propio por jugador y equipo, y una tabla de correspondencias por fuente (ver ADR 0001). El emparejamiento automático usa nombre normalizado, equipo, fecha de nacimiento y posición; los casos dudosos se escriben en un fichero de revisión que se aprueba a mano y queda versionado en git. Ninguna tabla curada admite filas sin ID canónico resuelto.

## Modelos

Dos modelos separados por sistema de puntuación (ver CONTEXT.md):

- **Modelo de minutos**: cuántos minutos jugará un jugador en la jornada (titularidad, rotación, lesiones, sanciones).
- **Modelo de rendimiento**: puntos por partido si juega, con variables de forma reciente, rival, local/visitante, posición y métricas de partido.

La proyección por jornada es el producto de ambos. Para fichajes sin historial en La Liga, el modelo de rendimiento se alimenta de sus estadísticas en la liga anterior y su valor en Transfermarkt, con un ajuste por el nivel de la liga de origen.

Enfoque en dos iteraciones: primero GLM jerárquico con statsmodels, después el mismo diseño en Stan (cmdstanpy) para incertidumbre completa. Validación: entrenar con temporadas pasadas y evaluar sobre la temporada siguiente (nunca mezclar futuro en el entrenamiento). Referencia mínima a batir: "los puntos de la próxima jornada son la media de las últimas cinco".

Cobertura de eventing: La Liga y Segunda (Biwenger + SofaScore), las 5 grandes ligas europeas completas, y el resto de ligas bajo demanda por jugador cuando aparece un fichaje. **Segunda es solo histórico** (decidido el 2026-07-10): alimenta los baselines de ascendidos, así que se backfillea y se re-ingiere completa una vez al cierre de cada temporada; durante la temporada no se refresca (bajo demanda si un jugador sube en el mercado de invierno).

## Aplicación web

Similar a `world-cup-predictor`: servidor FastAPI que sirve una página estática y endpoints JSON que leen directamente las tablas curadas de S3 (con DuckDB o pandas y caché local). Sin base de datos propia en fase 1. El diseño visual se hará con la skill `frontend-design` (la skill `/impeccable` mencionada no está disponible en este entorno).

Vistas de fase 1:

1. Tabla de proyecciones de la próxima jornada, filtrable por posición, equipo y precio.
2. Ficha de jugador: histórico de puntos, precio y proyección.
3. Mercado: mayores subidas/bajadas de precio y jugadores infravalorados.
4. Fichajes nuevos: baseline y comparación con jugadores similares ya conocidos.

## Infraestructura (AWS)

Región `eu-south-2` (España), cuenta 593760774245, perfil CLI `lfdata`, todo definido en Terraform (`infra/`).

- **Bucket S3** `lfdata-data-593760774245` para `raw/` y `curated/`.
- **Pipeline programado**: EventBridge lanza cada día una tarea de ECS Fargate que ingiere las fuentes, actualiza mappings, actualiza proyecciones y escribe en S3.
- **Web**: FastAPI en Lambda (Mangum) + estáticos en S3, detrás de CloudFront (App Runner no existe en `eu-south-2`).
- **Backfill inicial**: se lanza una vez, a mano, con el mismo código del pipeline.

Detalle en [implementation/07-infraestructura-aws.md](./implementation/07-infraestructura-aws.md).

## Estructura del repositorio

Paquete Python instalable con línea de comandos, como `world-cup-predictor`:

```
laliga-fantasy-data/
├── CONTEXT.md
├── docs/
│   ├── plan.md
│   └── adr/
├── pyproject.toml            # gestionado con uv
├── src/lfdata/
│   ├── sources/              # un módulo por fuente: biwenger, sofascore, transfermarkt
│   │   └── http.py           # transporte común: throttling, reintentos, proxy opcional
│   ├── storage/              # lectura/escritura S3, raw y curated
│   ├── mappings/             # matching automático + fichero de revisión manual
│   ├── features/             # construcción de variables para los modelos
│   ├── models/               # modelo de minutos, modelo de rendimiento
│   ├── projections/          # combinación de modelos → tabla de proyecciones
│   └── cli.py                # comandos: ingest, map, train, project, backfill
├── webapp/
│   ├── server.py
│   └── static/
└── tests/
```

La separación clave para soportar más plataformas de fantasy en el futuro: nada fuera de `sources/biwenger` conoce los nombres de campos de Biwenger; los sistemas de puntuación y los precios se guardan en tablas curadas con esquema neutro.

## Orden de construcción

Cada paso deja algo que funciona de principio a fin. Cada uno tiene su plan de implementación detallado en [docs/implementation/](./implementation/):

1. **Esqueleto + ingesta de Biwenger**: paquete, CLI, transporte HTTP, descarga de la temporada actual a `raw/` y primeras tablas curadas (jugadores, precios, puntos). Verificado (2026-07-07): la API expone los cinco sistemas de puntuación por jugador-partido, minutos, nota SofaScore e histórico de precios por temporada vía `season=YYYY`.
2. **Mappings**: ingesta de Transfermarkt (La Liga), matching automático, primer ciclo de revisión manual.
3. **SofaScore + backfill**: estadísticas por jugador-partido, 5 temporadas de La Liga, y verificación de FotMob como redundancia.
4. **Modelos**: referencia simple, luego modelo de minutos y modelo de rendimiento con validación temporal.
5. **Baseline de fichajes**: ingesta de ligas de origen y ajuste por nivel de liga.
6. **Web**: las cuatro vistas de fase 1.
7. **AWS**: bucket, pipeline diario programado y despliegue de la web.
8. **Fase 2** (decisiones pendientes): conexión de cuenta, alertas de cláusulas, optimización de alineación.

## Riesgos y comprobaciones pendientes

- **La API de Biwenger es no oficial**: puede cambiar sin aviso. La capa cruda y la validación de estructura limitan el daño.
- ~~Confirmar que Biwenger da los puntos históricos en varios sistemas de puntuación~~ — verificado el 2026-07-07: da los cinco por jugador-partido, también en temporadas pasadas.
- **Bloqueo de SofaScore**: si el throttling educado no basta, activar ScrapeOps (plan de pago) o promover FotMob a fuente primaria.
- ~~Verificar FotMob como fuente secundaria~~ — verificado el 2026-07-07 (ver `docs/experiments/2026-07-07-alex-fores.md`): viable con impersonación de Chrome; cobertura menor que SofaScore y nota no intercambiable.
- **Fase 2 sin resolver a propósito**: cómo autorizar el acceso a la liga privada del usuario sin pedirle la contraseña de Biwenger.
- **Cambio de temporada**: ascensos y descensos alteran plantillas y competiciones, y SofaScore estrena IDs de torneo-temporada. Cada julio requiere un paso manual asistido: actualizar IDs de temporada, ingerir los equipos ascendidos (su histórico de Segunda ya está en el bucket) y pasar una ronda de mappings de las plantillas nuevas.
- **Redistribución de datos de terceros**: la plataforma es abierta y muestra datos derivados de Transfermarkt y SofaScore. Mitigación de fase 1: mostrar valores agregados y derivados (proyecciones, tendencias) más que volcados crudos de las fuentes, citar la fuente, y revisar este riesgo antes de dar difusión pública a la web. No hay decisión definitiva tomada.
- **Nombre y dominio de la plataforma**: sin decidir; no bloquea nada hasta el despliegue público (paso 7).
