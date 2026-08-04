variable "bucket_name" {
  description = "Globally unique bucket name."
  type        = string
}

variable "versioning_enabled" {
  description = "Enable object versioning. Required for artifact rollback."
  type        = bool
  default     = true
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key for server-side encryption. Null uses SSE-S3."
  type        = string
  default     = null
}

variable "noncurrent_version_expiration_days" {
  description = "Days before a superseded object version is deleted."
  type        = number
  default     = 90
}

variable "abort_incomplete_upload_days" {
  description = "Days before an incomplete multipart upload is aborted."
  type        = number
  default     = 7
}

variable "tags" {
  description = "Tags applied to the bucket."
  type        = map(string)
  default     = {}
}
