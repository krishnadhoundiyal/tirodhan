variable "subscription_id" { type = string }
variable "tenant_id" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "suffix" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9]{4,10}$", var.suffix))
    error_message = "Use one stable lowercase alphanumeric NONPROD suffix (4-10 characters)."
  }
}
variable "postgres_admin_object_id" { type = string }
variable "postgres_admin_name" { type = string }
variable "postgres_admin_type" {
  type    = string
  default = "Group"
  validation {
    condition     = contains(["Group", "User", "ServicePrincipal"], var.postgres_admin_type)
    error_message = "Specify the Entra administrator principal type."
  }
}
variable "postgres_version" {
  type    = string
  default = "17"
}
variable "postgres_bootstrap_ipv4" {
  description = "Temporary operator/runner IPv4; null removes the bootstrap rule."
  type        = string
  default     = null
}
variable "deployment_stage" {
  description = "Monotonic enablement: 0 foundation, 1 migration, 2 API, 3 workers, 4 schedules."
  type        = number
  default     = 0
  validation {
    condition     = contains([0, 1, 2, 3, 4], var.deployment_stage)
    error_message = "deployment_stage must be 0..4. Never lower it on an established deployment."
  }
}
variable "application_image" {
  description = "Private GHCR application image tagged with the full commit SHA."
  type        = string
  default     = null
  validation {
    condition     = var.application_image == null ? true : can(regex("^ghcr.io/[a-z0-9_/-]+:[a-f0-9]{40}$", var.application_image))
    error_message = "Use a GHCR image tagged with a full lowercase Git SHA."
  }
}
variable "migration_image" {
  type    = string
  default = null
  validation {
    condition     = var.migration_image == null ? true : can(regex("^ghcr.io/[a-z0-9_/-]+:[a-f0-9]{40}$", var.migration_image))
    error_message = "Use a GHCR image tagged with a full lowercase Git SHA."
  }
}
variable "fluent_bit_image" {
  description = "Repository-built sidecar based on the pinned Fluent Bit base; SHA-tagged."
  type        = string
  default     = null
  validation {
    condition     = var.fluent_bit_image == null ? true : can(regex("^ghcr.io/[a-z0-9_/-]+:[a-f0-9]{40}$", var.fluent_bit_image))
    error_message = "Use a SHA-tagged GHCR sidecar image."
  }
}
variable "ghcr_username" { type = string }
variable "runtime_env" {
  description = "Explicit nonsecret TIRODHAN runtime values. No provider credentials/key material."
  type        = map(string)
  default     = {}
  validation {
    condition = alltrue([
      for key in keys(var.runtime_env) :
      contains(split("\n", replace(trimspace(file("${path.module}/../../../deploy/runtime-env.names")), "\r", "")), key)
    ])
    error_message = "runtime_env accepts only nonsecret TIRODHAN settings; secrets use Key Vault references."
  }
  validation {
    condition = var.deployment_stage < 2 || alltrue([
      for key in split("\n", replace(trimspace(file("${path.module}/../../../deploy/runtime-env.names")), "\r", "")) :
      key == "TIRODHAN_LOG_LEVEL" || try(length(trimspace(var.runtime_env[key])) > 0, false)
    ])
    error_message = "Configure all explicit runtime values in deploy/runtime-env.names before enabling the API/runtime workloads."
  }
}
variable "runtime_secret_names" {
  description = "Environment variable -> Key Vault secret name, never secret values."
  type        = map(string)
  default = {
    TIRODHAN_GOOGLE_MAPS_API_KEY      = "google-maps-api-key"
    TIRODHAN_KALEYRA_API_KEY          = "kaleyra-api-key"
    TIRODHAN_RAZORPAY_KEY_SECRET      = "razorpay-key-secret"
    TIRODHAN_RAZORPAY_WEBHOOK_SECRET  = "razorpay-webhook-secret"
    TIRODHAN_FCM_CREDENTIALS_JSON     = "fcm-credentials-json"
    TIRODHAN_AUTH_JWT_PRIVATE_KEY_PEM = "auth-jwt-private-key-pem"
    TIRODHAN_AUTH_JWT_PUBLIC_KEY_PEM  = "auth-jwt-public-key-pem"
    TIRODHAN_PHONE_ENCRYPTION_KEYS    = "phone-encryption-keys"
    TIRODHAN_PHONE_LOOKUP_HMAC_KEY    = "phone-lookup-hmac-key"
    TIRODHAN_ADDRESS_ENCRYPTION_KEYS  = "address-encryption-keys"
  }
}
variable "enabled_runtime_secret_names" {
  description = "Existing runtime Key Vault secret names to bind; names only, never values."
  type        = set(string)
  default     = []
  validation {
    condition = alltrue([
      for name in var.enabled_runtime_secret_names : contains(values(var.runtime_secret_names), name)
    ])
    error_message = "enabled_runtime_secret_names may contain only declared runtime secret names."
  }
}
variable "blob_soft_delete_days" {
  type = number
  validation {
    condition     = var.blob_soft_delete_days >= 1 && var.blob_soft_delete_days <= 365
    error_message = "Provide an approved soft-delete recovery window (1..365 days)."
  }
}
variable "media_cool_after_days" {
  type = number
  validation {
    condition     = var.media_cool_after_days >= 0
    error_message = "Provide an approved nonnegative media cool-tier threshold."
  }
}
variable "media_delete_after_days" {
  type = number
  validation {
    condition     = var.media_delete_after_days > var.media_cool_after_days
    error_message = "Approved media deletion must follow its cool-tier threshold."
  }
}
variable "logs_delete_after_days" {
  type = number
  validation {
    condition     = var.logs_delete_after_days > 0
    error_message = "Provide an approved positive log-retention window."
  }
}
variable "job_schedules" {
  type = map(string)
  default = {
    outbox                 = "* * * * *"
    planning_scheduler     = "* * * * *"
    fleet_timeout          = "* * * * *"
    pending_payment_expiry = "*/5 * * * *"
  }
  validation {
    condition     = toset(keys(var.job_schedules)) == toset(["outbox", "planning_scheduler", "fleet_timeout", "pending_payment_expiry"])
    error_message = "Configure the four existing finite schedules, evaluated in UTC."
  }
}
