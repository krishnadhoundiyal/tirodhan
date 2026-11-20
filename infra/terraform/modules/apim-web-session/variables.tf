variable "resource_group_name" {
  description = "Resource group of the existing NONPROD APIM gateway."
  type        = string
}

variable "gateway_name" {
  description = "The existing NONPROD Consumption gateway service name."
  type        = string
}

variable "gateway_principal_id" {
  description = "Object ID of the gateway's system-assigned managed identity."
  type        = string
}

variable "vault_resource_id" {
  description = "ARM resource ID of the bootstrap-owned NONPROD Key Vault."
  type        = string
}

variable "vault_uri" {
  description = "HTTPS base URI of the same Key Vault."
  type        = string
  validation {
    condition     = can(regex("^https://[a-z0-9-]+\\.vault\\.azure\\.net/?$", var.vault_uri))
    error_message = "Use a Key Vault base URI in Azure public cloud."
  }
}

variable "secret_name" {
  description = "Name only of the existing independent gateway invocation secret."
  type        = string
  validation {
    condition = (
      can(regex("^[A-Za-z0-9-]{1,127}$", var.secret_name)) &&
      !contains(["auth-jwt-private-key-pem", "auth-jwt-public-key-pem"], lower(var.secret_name))
    )
    error_message = "Supply the independent gateway secret name and keep JWT keys out of this binding."
  }
}

variable "backend_hostname" {
  description = "DNS hostname of the existing HTTPS FastAPI Container App ingress."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9-]+(\\.[a-z0-9-]+)+\\.azurecontainerapps\\.io$", var.backend_hostname))
    error_message = "Use the verified Azure Container Apps ingress hostname, without a scheme or path."
  }
}

variable "browser_origins" {
  description = "Exact approved Customer and Manager HTTPS origins."
  type        = set(string)
  validation {
    condition = length(var.browser_origins) == 2 && alltrue([
      for origin in var.browser_origins : can(regex("^https://[a-z0-9]+([.-][a-z0-9]+)*\\.[a-z]+$", origin))
    ])
    error_message = "Provide two distinct HTTPS origins without wildcard, port, path, query, or credentials."
  }
}

variable "web_policy_xml" {
  description = "Reviewed XML from the frontend renderer for the exact NONPROD configuration. Contains references only."
  type        = string
}

variable "web_policy_sha256" {
  description = "SHA-256 of the reviewed renderer output."
  type        = string
  validation {
    condition     = can(regex("^[a-f0-9]{64}$", var.web_policy_sha256))
    error_message = "Supply the lower-case SHA-256 of the reviewed XML."
  }
}

variable "business_operations" {
  description = "Reviewed exact FastAPI operations. Paths are relative to /v1. Native auth is managed separately."
  type = map(object({
    method = string
    path   = string
  }))
  validation {
    condition = alltrue([
      for key, operation in var.business_operations :
      can(regex("^[a-z][a-z0-9-]{0,70}$", key)) &&
      !startswith(key, "native-") &&
      contains(["GET", "POST", "PUT", "PATCH", "DELETE"], operation.method) &&
      can(regex("^/[a-z][a-z0-9-]*(/([a-zA-Z0-9_-]+|\\{[a-zA-Z][a-zA-Z0-9_]*\\}))*$", operation.path)) &&
      (
        !startswith("${operation.path}/", "/auth/") ||
        (operation.method == "GET" && operation.path == "/auth/me")
      )
    ])
    error_message = "Use explicit business routes only. The only allowed business auth route is GET /auth/me."
  }
}
