# Repositorio ECR de la imagen del pipeline (la misma que se usa en local).
# La construye y sube GitHub Actions al etiquetar versión (doc 07).

resource "aws_ecr_repository" "pipeline" {
  name                 = var.ecr_repository_name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Conserva las últimas 10 imágenes etiquetadas y caduca las sin etiqueta a los
# 7 días para que el repositorio no crezca sin límite.
resource "aws_ecr_lifecycle_policy" "pipeline" {
  repository = aws_ecr_repository.pipeline.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Caducar imágenes sin etiqueta a los 7 días"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Conservar solo las 10 imágenes etiquetadas más recientes"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      },
    ]
  })
}
