# Bucket de datos: capas raw/ y curated/, versionado y ciclo de vida (doc 07).
# También aloja el estado de Terraform bajo infra/terraform.tfstate.

resource "aws_s3_bucket" "data" {
  bucket = var.data_bucket_name
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket = aws_s3_bucket.data.id

  block_public_acls       = true
  block_public_policy      = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  # raw/ pasa a acceso infrecuente a los 90 días: casi nunca se re-lee, pero se
  # conserva como fuente de verdad reproducible.
  rule {
    id     = "raw-to-infrequent-access"
    status = "Enabled"

    filter {
      prefix = "raw/"
    }

    transition {
      days          = var.raw_infrequent_access_days
      storage_class = "STANDARD_IA"
    }
  }

  # Limpieza de versiones antiguas para que el versionado no infle el coste.
  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}
