########################################
# IAM role for Lambda functions
########################################
resource "aws_iam_role" "lambda_exec" {
  name = "hw10_lambda_exec_role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action = "sts:AssumeRole",
      Effect = "Allow",
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

########################################
# Lambda functions
########################################
resource "aws_lambda_function" "validate" {
  function_name    = "validateData"
  role             = aws_iam_role.lambda_exec.arn
  handler          = "validate.handler"
  runtime          = "python3.11"
  filename         = data.archive_file.validate.output_path
  source_code_hash = data.archive_file.validate.output_base64sha256
  timeout          = 30
}

resource "aws_lambda_function" "log_metrics" {
  function_name    = "logMetrics"
  role             = aws_iam_role.lambda_exec.arn
  handler          = "log_metrics.handler"
  runtime          = "python3.11"
  filename         = data.archive_file.log_metrics.output_path
  source_code_hash = data.archive_file.log_metrics.output_base64sha256
  timeout          = 30
}

########################################
# IAM role for Step Functions
########################################
resource "aws_iam_role" "stepfunction_exec" {
  name               = "hw10_stepfunction_exec_role"
  assume_role_policy = data.aws_iam_policy_document.stepfunction_trust.json
}

resource "aws_iam_role_policy" "stepfunction_invoke_lambda" {
  name = "hw10_stepfunction_invoke_lambda"
  role = aws_iam_role.stepfunction_exec.id

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect = "Allow",
        Action = "lambda:InvokeFunction",
        Resource = [
          aws_lambda_function.validate.arn,
          "${aws_lambda_function.validate.arn}:*",
          aws_lambda_function.log_metrics.arn,
          "${aws_lambda_function.log_metrics.arn}:*",
        ]
      }
    ]
  })
}

########################################
# Step Functions state machine
########################################
resource "aws_sfn_state_machine" "mlops_pipeline" {
  name     = var.state_machine_name
  role_arn = aws_iam_role.stepfunction_exec.arn

  definition = jsonencode({
    Comment = "MLOps HW10 — simplified training pipeline: ValidateData -> LogMetrics",
    StartAt = "ValidateData",
    States = {
      ValidateData = {
        Type     = "Task",
        Resource = aws_lambda_function.validate.arn,
        Retry = [
          {
            ErrorEquals     = ["States.ALL"],
            IntervalSeconds = 2,
            MaxAttempts     = 2,
            BackoffRate     = 2.0
          }
        ],
        Next = "LogMetrics"
      },
      LogMetrics = {
        Type     = "Task",
        Resource = aws_lambda_function.log_metrics.arn,
        Retry = [
          {
            ErrorEquals     = ["States.ALL"],
            IntervalSeconds = 2,
            MaxAttempts     = 2,
            BackoffRate     = 2.0
          }
        ],
        End = true
      }
    }
  })
}
