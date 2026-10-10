resource "azurerm_role_assignment" "bus_sender" {
  for_each             = { for key, queue in azurerm_servicebus_queue.work : key => queue if key != "financial_webhook" }
  scope                = each.value.id
  role_definition_name = "Azure Service Bus Data Sender"
  principal_id         = azurerm_user_assigned_identity.runtime.principal_id
}
resource "azurerm_role_assignment" "bus_receiver" {
  for_each             = { for key, queue in azurerm_servicebus_queue.work : key => queue if key != "financial_webhook" }
  scope                = each.value.id
  role_definition_name = "Azure Service Bus Data Receiver"
  principal_id         = azurerm_user_assigned_identity.runtime.principal_id
}
resource "azurerm_role_assignment" "financial_webhook_sender" {
  scope                = azurerm_servicebus_queue.work["financial_webhook"].id
  role_definition_name = "Azure Service Bus Data Sender"
  principal_id         = azurerm_user_assigned_identity.financial_webhook_sender.principal_id
}
resource "azurerm_role_assignment" "financial_webhook_receiver" {
  scope                = azurerm_servicebus_queue.work["financial_webhook"].id
  role_definition_name = "Azure Service Bus Data Receiver"
  principal_id         = azurerm_user_assigned_identity.financial_webhook_receiver.principal_id
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
