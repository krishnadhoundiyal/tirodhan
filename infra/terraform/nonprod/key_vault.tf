resource "azurerm_key_vault" "runtime" {
  name                       = "td-np-${var.suffix}-kv"
  resource_group_name        = data.azurerm_resource_group.nonprod.name
  location                   = var.location
  tenant_id                  = var.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = true
  tags                       = local.tags
}
