output "namespace_id" {
  value = local.namespace_id
}
output "queue_id" {
  value = azurerm_servicebus_queue.serviceability.id
}
output "worker_identity_id" {
  value = azurerm_user_assigned_identity.worker.id
}
output "publisher_identity_id" {
  value = azurerm_user_assigned_identity.publisher.id
}
