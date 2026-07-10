# Infraestructura AWS (Terraform)

Núcleo de infraestructura de La Liga Fantasy Data en `eu-south-2` (issue #4,
plan `docs/implementation/07-infraestructura-aws.md`). Terraform se aplica **a
mano desde local** con credenciales de administrador; nunca desde CI.

## Qué crea este núcleo

| Recurso | Detalle |
|---|---|
| S3 `lfdata-data-593760774245` | datos (`raw/`, `curated/`), versionado, cifrado, acceso público bloqueado; `raw/` pasa a acceso infrecuente a los 90 días; también aloja el estado de Terraform |
| IAM usuario `lfdata` | perfil CLI de permisos mínimos (lee/escribe solo el bucket de datos) |
| IAM rol `lfdata-pipeline` | rol de la futura tarea Fargate (mismos permisos sobre el bucket) |
| ECR `lfdata-pipeline` | repositorio de la imagen del pipeline |
| Presupuesto `lfdata-monthly` | alerta de facturación a 20 €/mes (avisos al 80% real y 100% previsto) |

Fuera de alcance aquí (llegan en pasos posteriores): bucket de estáticos,
Lambda web, CloudFront, tarea ECS/Fargate y regla de EventBridge.

## Requisitos

- [Terraform](https://developer.hashicorp.com/terraform) >= 1.10 (el bloqueo de
  estado usa el modo nativo de S3, sin DynamoDB).
- Credenciales de **administrador** de la cuenta `593760774245`, p. ej.
  `export AWS_PROFILE=lfdata-admin` (un perfil aparte del CLI `lfdata`).

## Arranque en dos pasos

El estado vive en el propio bucket de datos, pero este Terraform es quien lo
crea: hay que arrancar con estado local y migrarlo después.

```bash
cd infra

# 1) Primer apply con estado LOCAL (el backend "s3" de versions.tf va comentado)
terraform init
terraform plan
terraform apply

# 2) Descomentar el bloque backend "s3" en versions.tf y migrar el estado al bucket
terraform init -migrate-state
```

A partir de aquí, `terraform plan` debe salir **limpio** (la infra real coincide
con el código): ese es el criterio de aceptación del issue.

## Configurar el perfil CLI `lfdata`

Tras el primer apply, recupera las credenciales del usuario de permisos mínimos:

```bash
terraform output cli_access_key_id
terraform output -raw cli_secret_access_key
```

Configúralas en un perfil local (`~/.aws/credentials`) y comprueba el acceso:

```bash
aws configure --profile lfdata          # pega access key id y secret; región eu-south-2
AWS_PROFILE=lfdata aws s3 ls s3://lfdata-data-593760774245/
```

Después, el CLI escribe/lee contra el bucket apuntando `LFDATA_DATA` (backend S3
del issue #5):

```bash
export AWS_PROFILE=lfdata
export LFDATA_DATA="$(terraform output -raw data_uri)"
```

## Notas

- **Moneda del presupuesto**: `USD`. AWS Budgets solo admite la moneda de
  facturación de la cuenta, y esta (`593760774245`) solo acepta `USD`; el umbral
  son 20 USD/mes (≈18 €), suficiente como alerta blanda.
- El secreto de la access key queda en el estado de Terraform (cifrado, dentro
  del propio bucket). Es aceptable para un proyecto de un solo mantenedor; si se
  rota la clave, hazlo desde IAM y vuelve a aplicar.
