resource "azurerm_user_assigned_identity" "runtime" {
  name                = "${local.prefix}-runtime"
  resource_group_name = data.azurerm_resource_group.nonprod.name
  location            = var.location
  tags                = local.tags
}
