variable "name_prefix" {
  type = string
}
variable "resource_group_name" {
  type = string
}
variable "location" {
  type = string
}
variable "container_app_environment_id" {
  type = string
}
variable "image" {
  type        = string
  description = "Existing accessible GHCR image, pinned to the reviewed release. No registry is created."
}
variable "service_bus_namespace_name" {
  type = string
}
variable "existing_service_bus_namespace_id" {
  type        = string
  default     = null
  description = "Optional pre-existing Standard namespace ID; otherwise provision the named Standard namespace."
}
variable "queue_name" {
  type = string
}
variable "queue_lock_duration" {
  type = string
}
variable "queue_max_delivery_count" {
  type = number
}
variable "queue_message_ttl" {
  type = string
}
variable "publisher_schedule" {
  type        = string
  description = "Reviewed ACA Job cron schedule (UTC)."
}
variable "publisher_batch_size" {
  type = number
  validation {
    condition     = var.publisher_batch_size > 0 && floor(var.publisher_batch_size) == var.publisher_batch_size
    error_message = "Publisher batch size must be a positive integer."
  }
}
variable "publisher_timeout_seconds" {
  type = number
}
variable "service_bus_timeout_seconds" {
  type = number
}
variable "worker_max_replicas" {
  type = number
}
variable "worker_lock_renewal_seconds" {
  type = number
}
variable "google_timeout_seconds" {
  type = number
}
variable "delhi_admin_aliases" {
  type    = list(string)
  default = ["Delhi", "DL", "National Capital Territory of Delhi", "NCT of Delhi"]
}
variable "address_encryption_active_key_id" {
  type = string
}
variable "key_vault_id" {
  type        = string
  description = "Existing RBAC-enabled vault resource ID. Secret values are provisioned separately, never read into Terraform."
}
variable "database_url_secret_uri" {
  type        = string
  description = "Existing Key Vault secret URI holding PostgreSQL+asyncpg TLS connection config."
}
variable "google_api_key_secret_uri" {
  type = string
}
variable "address_keys_secret_uri" {
  type = string
}
variable "environment" {
  type = string
  validation {
    condition     = contains(["nonprod", "prod"], var.environment)
    error_message = "Permanent hosted environments are nonprod and prod."
  }
}
