resource "azurerm_user_assigned_identity" "runtime" {
  name                = "${local.prefix}-runtime"
  resource_group_name = data.azurerm_resource_group.nonprod.name
  location            = var.location
  tags                = local.tags
}
resource "azurerm_user_assigned_identity" "financial_webhook_sender" {
  name                = "${local.prefix}-finhook-send"
  resource_group_name = data.azurerm_resource_group.nonprod.name
  location            = var.location
  tags                = local.tags
}
resource "azurerm_user_assigned_identity" "financial_webhook_receiver" {
  name                = "${local.prefix}-finhook-recv"
  resource_group_name = data.azurerm_resource_group.nonprod.name
  location            = var.location
  tags                = local.tags
}
