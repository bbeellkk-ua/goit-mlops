########################################
# Trust policy for Step Functions
########################################
data "aws_iam_policy_document" "stepfunction_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

########################################
# Lambda source packaging
########################################
data "archive_file" "validate" {
  type        = "zip"
  source_file = "${path.module}/lambda/validate.py"
  output_path = "${path.module}/${var.lambda_validate_zip}"
}

data "archive_file" "log_metrics" {
  type        = "zip"
  source_file = "${path.module}/lambda/log_metrics.py"
  output_path = "${path.module}/${var.lambda_log_metrics_zip}"
}
