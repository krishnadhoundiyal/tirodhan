output "deployment_stage" { value = var.deployment_stage }
output "application_image" { value = var.application_image }
output "migration_image" { value = var.migration_image }
output "fluent_bit_image" { value = var.fluent_bit_image }
output "resource_group_name" { value = data.azurerm_resource_group.nonprod.name }
output "runtime_client_id" { value = azurerm_user_assigned_identity.runtime.client_id }
output "runtime_principal_id" { value = azurerm_user_assigned_identity.runtime.principal_id }
output "runtime_database_role" { value = azurerm_user_assigned_identity.runtime.name }
output "postgres_host" { value = azurerm_postgresql_flexible_server.database.fqdn }
output "key_vault_name" { value = azurerm_key_vault.runtime.name }
output "key_vault_url" { value = azurerm_key_vault.runtime.vault_uri }
output "storage_account_name" { value = azurerm_storage_account.application.name }
output "storage_account_id" { value = azurerm_storage_account.application.id }
output "api_url" { value = var.deployment_stage >= 2 ? "https://${azurerm_container_app.api[0].ingress[0].fqdn}" : null }
output "migration_job_name" { value = var.deployment_stage >= 1 ? azurerm_container_app_job.migration[0].name : null }
