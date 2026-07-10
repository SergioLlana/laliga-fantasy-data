terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # El estado vive en el propio bucket de datos (issue #4, doc 07).
  # Bloqueo nativo de S3 (use_lockfile), sin tabla DynamoDB — requiere Terraform >= 1.10.
  backend "s3" {
    bucket       = "lfdata-data-593760774245"
    key          = "infra/terraform.tfstate"
    region       = "eu-south-2"
    encrypt      = true
    use_lockfile = true
  }
}
