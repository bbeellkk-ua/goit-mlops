variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "eu-west-1"
}

variable "aws_profile" {
  description = "AWS CLI profile used by Terraform for the local backend/provider"
  type        = string
  default     = "goit-terraform"
}

variable "state_machine_name" {
  description = "Name of the Step Functions state machine"
  type        = string
  default     = "MLOpsPipeline"
}

variable "lambda_validate_zip" {
  description = "Relative path to the validate lambda ZIP archive"
  type        = string
  default     = "lambda/validate.zip"
}

variable "lambda_log_metrics_zip" {
  description = "Relative path to the log_metrics lambda ZIP archive"
  type        = string
  default     = "lambda/log_metrics.zip"
}
