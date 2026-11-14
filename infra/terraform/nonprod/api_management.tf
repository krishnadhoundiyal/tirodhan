resource "azurerm_api_management" "nonprod" {
  count               = var.deployment_stage >= 4 && var.apim_publisher_email != null ? 1 : 0
  name                = "${local.prefix}-apim"
  location            = var.location
  resource_group_name = data.azurerm_resource_group.nonprod.name
  publisher_name      = "Tirodhan"
  publisher_email     = var.apim_publisher_email
  sku_name            = "Consumption_0"
  tags                = local.tags
}
