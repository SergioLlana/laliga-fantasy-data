# Paso 7 — Infraestructura AWS y pipeline diario

**Objetivo:** todo corre solo: ingesta diaria, mappings, proyecciones y web pública, definido en Terraform y desplegado en `eu-south-2`.

## Decisiones ya tomadas que aplican aquí

- Cuenta 593760774245, perfil CLI nuevo `lfdata` con permisos mínimos, bucket `lfdata-data-593760774245` (región `eu-south-2`, España).
- Terraform en `infra/` para toda la infraestructura.
- Web: Lambda (Mangum) + estáticos en S3 + CloudFront (App Runner no existe en `eu-south-2`).

## Piezas de Terraform

| Recurso | Detalle |
|---|---|
| S3 `lfdata-data-*` | datos (`raw/`, `curated/`), versionado activado, ciclo de vida: `raw/` a almacenamiento infrecuente a los 90 días |
| S3 `lfdata-site-*` | estáticos de la web (sigue la convención de wcpred) |
| IAM | rol del pipeline (lee/escribe el bucket de datos), rol de la Lambda web (solo lectura de `curated/`), usuario CLI `lfdata` |
| ECR + tarea ECS Fargate | imagen del pipeline (la misma que se usa en local); Fargate y no Lambda porque el paso diario completo supera cómodamente los 15 min de límite de Lambda |
| EventBridge | regla diaria 06:00 Europe/Madrid → lanza la tarea Fargate |
| Lambda + Function URL | la API FastAPI vía Mangum |
| CloudFront | una distribución: `/` y estáticos desde S3, `/api/*` a la Lambda |
| Presupuesto | alerta de facturación a 20 €/mes |

Coste esperado en reposo: <5 €/mes (S3 céntimos, Fargate ~15 min/día, Lambda y CloudFront en franja gratuita con poco tráfico).

## El trabajo diario (orquestación)

Un solo comando, `lfdata daily`, ejecuta en orden dentro de la tarea Fargate:

1. `ingest biwenger` (La Liga y Segunda, día actual: plantillas, precios, reports de la última jornada).
2. `ingest sofascore` (partidos nuevos de las ligas cubiertas + bajo demanda de jugadores nuevos detectados).
3. `ingest transfermarkt` (semanal: valores y traspasos; no cambia a diario).
4. `map --check` (falla el trabajo si aparecen filas sin mapping → llegan a revisión manual).
5. `evaluate --last-round` si la jornada acaba de terminar (compara las proyecciones de **todas las versiones vivas** contra los puntos reales → `projection_accuracy`, ver paso 4).
6. `project --round siguiente` con la versión activa **y con cada candidato vivo** (proyección en la sombra), cada una a su partición de `projections`.

Además, un trabajo **semanal** (tras jornada) reentrena y publica una versión nueva como `candidate`; la promoción a `active` es siempre manual (`lfdata models activate`, ver el registro de modelos en el paso 4).

Sin orquestador externo (Airflow, Step Functions): es una secuencia lineal donde si un paso falla, se corta y se notifica (SNS → email). La re-ejecución es idempotente porque `raw/` no se re-descarga y `curated/` se sobrescribe por partición.

## Despliegues

- GitHub Actions: al hacer push a `main`, lint + tests; al etiquetar versión, construir y subir imagen a ECR, empaquetar la Lambda y sincronizar estáticos.
- Terraform se aplica a mano desde local (`infra/` con estado en el propio bucket) — la infra cambia poco y así no hay credenciales de administrador en CI.

## Orden de trabajo

1. Terraform del núcleo: bucket de datos, IAM, ECR.
2. Contenedor del pipeline + tarea manual en Fargate (backfills reales ya se lanzan así).
3. EventBridge diario + notificación de fallos.
4. Lambda web + CloudFront + estáticos.
5. Dominio y certificado si se quiere URL propia (decisión pendiente, no bloquea).

## Hecho cuando

- Tres días seguidos de ejecución diaria sin intervención, con proyecciones actualizadas en la web pública.
- Un fallo provocado (p. ej. cambiar el formato esperado de Biwenger en un test de humo) corta el trabajo y llega el aviso por email.
- `terraform plan` limpio = la infra real coincide con el código.
