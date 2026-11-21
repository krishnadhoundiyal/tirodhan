resource "azurerm_api_management" "nonprod" {
  count               = var.deployment_stage >= 4 && var.apim_publisher_email != null ? 1 : 0
  name                = "${local.prefix}-apim"
  location            = var.location
  resource_group_name = data.azurerm_resource_group.nonprod.name
  publisher_name      = "Tirodhan"
  publisher_email     = var.apim_publisher_email
  sku_name            = "Consumption_0"
  tags                = local.tags

  dynamic "identity" {
    for_each = var.apim_web_session == null ? [] : [1]
    content {
      type = "SystemAssigned"
    }
  }
}

data "azurerm_api_management" "web_session_gateway" {
  count = var.apim_web_session == null ? 0 : 1

  name                = azurerm_api_management.nonprod[0].name
  resource_group_name = data.azurerm_resource_group.nonprod.name

  # AzureRM can report a null principal while planning an in-place identity enablement.
  depends_on = [azurerm_api_management.nonprod]
}

module "apim_web_session" {
  source = "../modules/apim-web-session"
  count  = var.apim_web_session == null ? 0 : 1

  resource_group_name  = data.azurerm_resource_group.nonprod.name
  gateway_name         = azurerm_api_management.nonprod[0].name
  gateway_resource_id  = azurerm_api_management.nonprod[0].id
  gateway_principal_id = data.azurerm_api_management.web_session_gateway[0].identity[0].principal_id
  vault_resource_id    = data.azurerm_key_vault.runtime.id
  vault_uri            = data.azurerm_key_vault.runtime.vault_uri
  secret_name          = var.apim_web_session.secret_name
  backend_hostname     = var.apim_web_session.backend_hostname
  browser_origins      = var.apim_web_session.browser_origins
  web_policy_xml       = var.apim_web_session.web_policy_xml
  web_policy_sha256    = var.apim_web_session.web_policy_sha256
  web_policy_fragments = var.apim_web_policy_fragments
  business_operations  = var.apim_web_session.business_operations
}

resource "azurerm_api_management" "pilot" {
  count               = var.deployment_stage >= 4 && var.apim_publisher_email != null && var.apim_pilot_enabled ? 1 : 0
  name                = "${local.prefix}-apim-pilot"
  location            = var.location
  resource_group_name = data.azurerm_resource_group.nonprod.name
  publisher_name      = "Tirodhan"
  publisher_email     = var.apim_publisher_email
  sku_name            = "Consumption_0"
  tags                = local.tags

  identity {
    type = "SystemAssigned"
  }
}
