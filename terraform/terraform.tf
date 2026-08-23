terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 6.0"
    }

    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4.0"
    }
  }

  # Remote state — S3 backend.
  # NOTE: the S3 backend does not accept HCL variables, so bucket/profile/region
  # are hardcoded here. Adjust these values to your own AWS account/profile
  # when reusing this project.
  backend "s3" {
    bucket  = "goit-terraform-denys-bieliaiev"
    key     = "step-function/terraform.tfstate"
    region  = "eu-west-1"
    encrypt = true
    profile = "goit-terraform"
  }
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project   = "mlops-course"
      Homework  = "hw10-step-functions"
      ManagedBy = "terraform"
    }
  }
}
