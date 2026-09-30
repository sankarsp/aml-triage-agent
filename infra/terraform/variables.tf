variable "project" {
  type    = string
  default = "aml-triage"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "image_tag" {
  description = "Tag of the image in ECR to deploy"
  type        = string
  default     = "latest"
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "task_cpu" {
  type    = number
  default = 1024
}

variable "task_memory" {
  type    = number
  default = 2048
}

variable "bedrock_model_id" {
  description = "Bedrock model or inference-profile ID for the analyst LLM (enable access in the Bedrock console first)"
  type        = string
  default     = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
}

variable "bedrock_embedding_model_id" {
  type    = string
  default = "amazon.titan-embed-text-v2:0"
}

variable "embedding_dim" {
  type    = number
  default = 1024
}

variable "db_engine_version" {
  description = "Aurora PostgreSQL version (must support pgvector 0.7+)"
  type        = string
  default     = "16.6"
}

variable "db_min_acu" {
  type    = number
  default = 0.5
}

variable "db_max_acu" {
  type    = number
  default = 4
}

variable "allowed_ingress_cidrs" {
  description = "CIDRs allowed to reach the load balancer. Restrict to your office/VPN for anything real."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "certificate_arn" {
  description = "ACM certificate ARN for HTTPS. Empty = HTTP only (dev)."
  type        = string
  default     = ""
}

variable "enable_bedrock_vpc_endpoint" {
  description = "Keep Bedrock traffic on the AWS network via a PrivateLink endpoint"
  type        = bool
  default     = true
}

variable "force_escalate_score" {
  type    = number
  default = 70
}

variable "auto_close_max_score" {
  type    = number
  default = -1
}

variable "high_risk_countries" {
  type    = string
  default = "KP,IR,MM"
}
