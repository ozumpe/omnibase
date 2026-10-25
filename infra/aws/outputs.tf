output "instance_id" {
  description = "null with create_instance = false; the scheduled run's box is infra/aws/box"
  value       = one(module.box[*].instance_id)
}

output "security_group_id" {
  value = aws_security_group.egress_only.id
}

output "instance_profile" {
  value = aws_iam_instance_profile.instance.name
}

output "artifacts_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "secret_name" {
  value = aws_secretsmanager_secret.credentials.name
}

output "put_secret_hint" {
  value = "poetry run python scripts/aws_secret.py --upload --secret-id ${aws_secretsmanager_secret.credentials.name} --region ${var.region}"
}

output "ssm_session" {
  value = "aws ssm start-session --target ${coalesce(one(module.box[*].instance_id), "<no box>")} --region ${var.region}"
}

output "frontend_port_forward" {
  value = "aws ssm start-session --target ${coalesce(one(module.box[*].instance_id), "<no box>")} --region ${var.region} --document-name AWS-StartPortForwardingSession --parameters '{\"portNumber\":[\"8080\"],\"localPortNumber\":[\"8080\"]}'"
}

output "alerts_topic_arn" {
  description = "The pager topic; exported on the box as SIS_NOTIFY_SNS_TOPIC_ARN"
  value       = aws_sns_topic.alerts.arn
}
