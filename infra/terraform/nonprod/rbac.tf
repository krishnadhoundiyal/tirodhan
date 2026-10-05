resource "azurerm_role_assignment" "bus_sender" {
  for_each             = azurerm_servicebus_queue.work
  scope                = each.value.id
  role_definition_name = "Azure Service Bus Data Sender"
  principal_id         = azurerm_user_assigned_identity.runtime.principal_id
}
resource "azurerm_role_assignment" "bus_receiver" {
  for_each             = azurerm_servicebus_queue.work
  scope                = each.value.id
  role_definition_name = "Azure Service Bus Data Receiver"
  principal_id         = azurerm_user_assigned_identity.runtime.principal_id
}
resource "azurerm_role_assignment" "media" {
  scope                = azurerm_storage_container.private["media"].id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.runtime.principal_id
}
resource "azurerm_role_assignment" "delegation" {
  scope                = azurerm_storage_account.application.id
  role_definition_name = "Storage Blob Delegator"
  principal_id         = azurerm_user_assigned_identity.runtime.principal_id
}
resource "azurerm_role_assignment" "secrets" {
  scope                = azurerm_key_vault.runtime.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.runtime.principal_id
}
