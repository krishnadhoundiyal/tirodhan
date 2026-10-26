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

# This identity has elevated PostgreSQL authority ONLY for finite bootstrap execution.
# Never attach it to the API, scheduled jobs, or long-running message consumers.
resource "azurerm_user_assigned_identity" "postgres_bootstrap" {
  name                = "${local.prefix}-pgboot"
  resource_group_name = data.azurerm_resource_group.nonprod.name
  location            = var.location
  tags                = local.tags
}
