terraform {
  required_providers {
    azurerm = {
      source = "hashicorp/azurerm"
    }
  }
}

locals {
  backend_url = "https://${var.backend_hostname}"
  native = {
    "native-otp-start"  = { method = "POST", path = "/auth/otp/start" }
    "native-otp-verify" = { method = "POST", path = "/auth/otp/verify" }
    "native-refresh"    = { method = "POST", path = "/auth/refresh" }
    "native-logout"     = { method = "POST", path = "/auth/logout" }
  }
  web_paths = {
    start   = "/otp/start"
    verify  = "/otp/verify"
    refresh = "/refresh"
    logout  = "/logout"
  }
  web_operations = merge(
    { for name, path in local.web_paths : "${name}-post" => { method = "POST", path = path } },
    { for name, path in local.web_paths : "${name}-options" => { method = "OPTIONS", path = path } },
  )
}

resource "azurerm_role_assignment" "gateway_secret" {
  scope                = "${var.vault_resource_id}/secrets/${var.secret_name}"
  role_definition_name = "Key Vault Secrets User"
  principal_id         = var.gateway_principal_id
  principal_type       = "ServicePrincipal"

  lifecycle {
    precondition {
      condition = (
        var.gateway_name == "${var.resource_group_name}-apim-pilot" &&
        can(regex("^/subscriptions/[0-9a-f-]+/resourcegroups/${var.resource_group_name}/providers/microsoft\\.keyvault/vaults/[a-z0-9-]+$", lower(var.vault_resource_id))) &&
        trimsuffix(var.vault_uri, "/") == "https://${basename(lower(var.vault_resource_id))}.vault.azure.net"
      )
      error_message = "The managed gateway and the matching vault ID and URI must refer to the same NONPROD resource group."
    }
  }
}

resource "azurerm_api_management_named_value" "gateway_secret" {
  name                = "gateway-secret"
  display_name        = "gateway-secret"
  resource_group_name = var.resource_group_name
  api_management_name = var.gateway_name
  secret              = true

  value_from_key_vault {
    secret_id = "${trimsuffix(var.vault_uri, "/")}/secrets/${var.secret_name}"
  }

  depends_on = [azurerm_role_assignment.gateway_secret]
}

resource "azurerm_api_management_api" "web" {
  name                  = "tirodhan-web-session"
  resource_group_name   = var.resource_group_name
  api_management_name   = var.gateway_name
  revision              = "1"
  display_name          = "Tirodhan browser session"
  path                  = "web/session"
  protocols             = ["https"]
  subscription_required = false
}

resource "azurerm_api_management_api_policy" "web" {
  api_name            = azurerm_api_management_api.web.name
  api_management_name = var.gateway_name
  resource_group_name = var.resource_group_name
  xml_content         = var.web_policy_xml

  lifecycle {
    precondition {
      condition = (
        sha256(var.web_policy_xml) == var.web_policy_sha256 &&
        length(regexall("<set-header name=\"X-Gateway-Secret\" exists-action=\"override\"><value>\\{\\{gateway-secret\\}\\}</value></set-header>", var.web_policy_xml)) == 1 &&
        length(regexall("<base\\s*/>", var.web_policy_xml)) == 0
      )
      error_message = "Use the reviewed isolated frontend renderer output with exactly one gateway named-value override."
    }
  }

  depends_on = [azurerm_api_management_named_value.gateway_secret]
}

resource "azurerm_api_management_api_operation" "web" {
  for_each            = local.web_operations
  operation_id        = each.key
  api_name            = azurerm_api_management_api.web.name
  api_management_name = var.gateway_name
  resource_group_name = var.resource_group_name
  display_name        = each.key
  method              = each.value.method
  url_template        = each.value.path

  # Register routes only after the isolated response-shaping policy is present.
  depends_on = [azurerm_api_management_api_policy.web]
}

resource "azurerm_api_management_api" "backend" {
  name                  = "tirodhan-backend-v1"
  resource_group_name   = var.resource_group_name
  api_management_name   = var.gateway_name
  revision              = "1"
  display_name          = "Tirodhan API v1"
  path                  = "v1"
  protocols             = ["https"]
  service_url           = "${local.backend_url}/v1"
  subscription_required = false
}

resource "azurerm_api_management_api_policy" "backend" {
  api_name            = azurerm_api_management_api.backend.name
  api_management_name = var.gateway_name
  resource_group_name = var.resource_group_name
  xml_content = templatefile("${path.module}/backend-policy.xml.tftpl", {
    browser_origins = sort(tolist(var.browser_origins))
  })

  depends_on = [azurerm_api_management_named_value.gateway_secret]
}

resource "azurerm_api_management_api_operation" "backend" {
  for_each            = merge(local.native, var.business_operations)
  operation_id        = each.key
  api_name            = azurerm_api_management_api.backend.name
  api_management_name = var.gateway_name
  resource_group_name = var.resource_group_name
  display_name        = each.key
  method              = each.value.method
  url_template        = each.value.path

  dynamic "template_parameter" {
    for_each = toset(flatten(regexall("\\{([A-Za-z][A-Za-z0-9_]*)\\}", each.value.path)))
    content {
      name     = template_parameter.value
      type     = "string"
      required = true
    }
  }

  depends_on = [azurerm_api_management_api_policy.backend]
}
