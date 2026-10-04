resource "azurerm_container_app_environment" "nonprod" {
  name                = "${local.prefix}-aca"
  resource_group_name = data.azurerm_resource_group.nonprod.name
  location            = var.location
  # Modern workload-profile environment, Consumption only. No customer VNET.
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
  # No logs_destination/Log Analytics workspace: Blob sidecars are authoritative.
  tags = local.tags
}
