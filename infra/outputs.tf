output "data_bucket_name" {
  description = "Nombre del bucket de datos (valor de LFDATA_DATA sin el esquema s3://)."
  value       = aws_s3_bucket.data.bucket
}

output "data_uri" {
  description = "URI base de almacenamiento para el CLI: export LFDATA_DATA=<esto>."
  value       = "s3://${aws_s3_bucket.data.bucket}"
}

output "ecr_repository_url" {
  description = "URL del repositorio ECR del pipeline."
  value       = aws_ecr_repository.pipeline.repository_url
}

output "pipeline_role_arn" {
  description = "ARN del rol de la tarea del pipeline."
  value       = aws_iam_role.pipeline.arn
}

output "cli_access_key_id" {
  description = "Access key ID del usuario CLI `lfdata`."
  value       = aws_iam_access_key.cli.id
}

output "cli_secret_access_key" {
  description = "Secret access key del usuario CLI `lfdata` (configúralo en el perfil AWS local)."
  value       = aws_iam_access_key.cli.secret
  sensitive   = true
}
