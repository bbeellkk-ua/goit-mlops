output "lambda_validate_arn" {
  description = "ARN of the validateData Lambda function"
  value       = aws_lambda_function.validate.arn
}

output "lambda_log_metrics_arn" {
  description = "ARN of the logMetrics Lambda function"
  value       = aws_lambda_function.log_metrics.arn
}

output "stepfunction_arn" {
  description = "ARN of the MLOpsPipeline Step Functions state machine"
  value       = aws_sfn_state_machine.mlops_pipeline.arn
}

output "stepfunction_name" {
  description = "Name of the Step Functions state machine"
  value       = aws_sfn_state_machine.mlops_pipeline.name
}
