output "api_url" {
  value = "${local.https ? "https" : "http"}://${aws_lb.app.dns_name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "api_key_secret_arn" {
  description = "Read with: aws secretsmanager get-secret-value --secret-id <arn> --query SecretString --output text"
  value       = aws_secretsmanager_secret.api_key.arn
}

output "db_endpoint" {
  value = aws_rds_cluster.main.endpoint
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "seed_command" {
  description = "One-off task that creates the schema, loads the knowledge base and demo data"
  value = join(" ", [
    "aws ecs run-task --region ${var.region} --cluster ${aws_ecs_cluster.main.name}",
    "--launch-type FARGATE --task-definition ${aws_ecs_task_definition.app.family}",
    "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", module.vpc.private_subnets)}],securityGroups=[${aws_security_group.app.id}],assignPublicIp=DISABLED}'",
    "--overrides '{\"containerOverrides\":[{\"name\":\"api\",\"command\":[\"python\",\"-m\",\"scripts.seed\"]}]}'",
  ])
}
