# The purge-protected vault is bootstrap-owned and persists across NONPROD teardown.
# Never re-create or destroy it from the disposable application Terraform state.
data "azurerm_key_vault" "runtime" {
  name                = "td-np-${var.suffix}-kv"
  resource_group_name = data.azurerm_resource_group.nonprod.name
}
