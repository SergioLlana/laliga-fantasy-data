provider "aws" {
  region = var.region

  # Aplicar Terraform con credenciales de administrador (perfil aparte del CLI
  # `lfdata`, que es de permisos mínimos). El bloqueo evita apuntar a otra cuenta.
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project   = "laliga-fantasy-data"
      ManagedBy = "terraform"
    }
  }
}
