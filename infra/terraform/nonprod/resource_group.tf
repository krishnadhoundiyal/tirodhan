# Created by the separately authorized bootstrap; one owner, no duplicate state.
data "azurerm_resource_group" "nonprod" {
  name = var.resource_group_name
}
