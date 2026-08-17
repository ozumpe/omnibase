output "instance_id" {
  value = aws_instance.sis.id
}

output "artifacts_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "secret_name" {
  value = aws_secretsmanager_secret.credentials.name
}

output "put_secret_hint" {
  value = "aws secretsmanager put-secret-value --secret-id ${aws_secretsmanager_secret.credentials.name} --region ${var.region} --secret-string file://secrets.aws.json"
}

output "ssm_session" {
  value = "aws ssm start-session --target ${aws_instance.sis.id} --region ${var.region}"
}

output "frontend_port_forward" {
  value = "aws ssm start-session --target ${aws_instance.sis.id} --region ${var.region} --document-name AWS-StartPortForwardingSession --parameters '{\"portNumber\":[\"8080\"],\"localPortNumber\":[\"8080\"]}'"
}
