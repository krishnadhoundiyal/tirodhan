resource "azurerm_servicebus_namespace" "bus" {
  name                = "${local.prefix}-bus"
  resource_group_name = data.azurerm_resource_group.nonprod.name
  location            = var.location
  sku                 = "Standard"
  local_auth_enabled  = false
  minimum_tls_version = "1.2"
  tags                = local.tags
}
resource "azurerm_servicebus_queue" "work" {
  for_each                     = local.queues
  name                         = each.value
  namespace_id                 = azurerm_servicebus_namespace.bus.id
  requires_session             = each.key == "planning"
  requires_duplicate_detection = false
  max_delivery_count           = 10
  lock_duration                = "PT1M"
}
