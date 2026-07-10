variable "aws_account_id" {
  description = "Cuenta AWS donde vive toda la infraestructura."
  type        = string
  default     = "593760774245"
}

variable "region" {
  description = "Región de despliegue (España)."
  type        = string
  default     = "eu-south-2"
}

variable "data_bucket_name" {
  description = "Bucket de datos: capas raw/ y curated/."
  type        = string
  default     = "lfdata-data-593760774245"
}

variable "ecr_repository_name" {
  description = "Repositorio ECR de la imagen del pipeline."
  type        = string
  default     = "lfdata-pipeline"
}

variable "cli_user_name" {
  description = "Usuario IAM de permisos mínimos para el CLI local `lfdata`."
  type        = string
  default     = "lfdata"
}

variable "budget_limit_amount" {
  description = "Umbral de la alerta de presupuesto mensual."
  type        = string
  default     = "20"
}

variable "budget_currency" {
  description = <<-EOT
    Moneda del presupuesto. AWS Budgets solo admite la moneda de facturación de
    la cuenta; esta cuenta (593760774245) solo acepta "USD".
  EOT
  type        = string
  default     = "USD"
}

variable "budget_notification_email" {
  description = "Correo que recibe la alerta de presupuesto."
  type        = string
  default     = "sergio.llanaperez@gmail.com"
}

variable "raw_infrequent_access_days" {
  description = "Días tras los que raw/ pasa a almacenamiento de acceso infrecuente."
  type        = number
  default     = 90
}
