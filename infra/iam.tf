# Permisos mínimos sobre el bucket de datos, compartidos por el usuario CLI y el
# rol del pipeline: listar el bucket y leer/escribir/borrar objetos, nada más.

data "aws_iam_policy_document" "data_bucket_access" {
  statement {
    sid       = "ListDataBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.data.arn]
  }

  statement {
    sid    = "ReadWriteDataObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.data.arn}/*"]
  }
}

resource "aws_iam_policy" "data_bucket_access" {
  name        = "lfdata-data-bucket-access"
  description = "Lectura/escritura sobre el bucket de datos de lfdata."
  policy      = data.aws_iam_policy_document.data_bucket_access.json
}

# --- Usuario CLI local `lfdata` (perfil de permisos mínimos) ------------------

resource "aws_iam_user" "cli" {
  name = var.cli_user_name
}

resource "aws_iam_user_policy_attachment" "cli_data_bucket" {
  user       = aws_iam_user.cli.name
  policy_arn = aws_iam_policy.data_bucket_access.arn
}

# Clave de acceso para configurar el perfil AWS local. El secreto queda en el
# estado (cifrado, en el propio bucket); recupéralo con `terraform output`.
resource "aws_iam_access_key" "cli" {
  user = aws_iam_user.cli.name
}

# --- Rol del pipeline (tarea Fargate, se materializa en pasos posteriores) ----

data "aws_iam_policy_document" "pipeline_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "pipeline" {
  name               = "lfdata-pipeline"
  description        = "Rol de la tarea del pipeline diario: lee/escribe el bucket de datos."
  assume_role_policy = data.aws_iam_policy_document.pipeline_assume.json
}

resource "aws_iam_role_policy_attachment" "pipeline_data_bucket" {
  role       = aws_iam_role.pipeline.name
  policy_arn = aws_iam_policy.data_bucket_access.arn
}
