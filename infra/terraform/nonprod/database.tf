resource "azurerm_postgresql_flexible_server" "database" {
  name                          = "${local.prefix}-pg"
  resource_group_name           = data.azurerm_resource_group.nonprod.name
  location                      = var.location
  version                       = var.postgres_version
  sku_name                      = "B_Standard_B1ms"
  storage_mb                    = 32768
  backup_retention_days         = 7
  geo_redundant_backup_enabled  = false
  public_network_access_enabled = true
  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false
    tenant_id                     = var.tenant_id
  }
  tags = local.tags
}
resource "azurerm_postgresql_flexible_server_database" "application" {
  name      = "tirodhan"
  server_id = azurerm_postgresql_flexible_server.database.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}
resource "azurerm_postgresql_flexible_server_active_directory_administrator" "administrator" {
  server_name         = azurerm_postgresql_flexible_server.database.name
  resource_group_name = data.azurerm_resource_group.nonprod.name
  tenant_id           = var.tenant_id
  object_id           = var.postgres_admin_object_id
  principal_name      = var.postgres_admin_name
  principal_type      = var.postgres_admin_type
}
resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.database.id
  value     = "postgis"
}
# Explicitly approved broad Azure-origin exception for NONPROD, not PROD.
resource "azurerm_postgresql_flexible_server_firewall_rule" "azure_nonprod" {
  name             = "nonprod-azure-origin"
  server_id        = azurerm_postgresql_flexible_server.database.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}
resource "azurerm_postgresql_flexible_server_firewall_rule" "bootstrap" {
  count            = var.postgres_bootstrap_ipv4 == null ? 0 : 1
  name             = "temporary-bootstrap"
  server_id        = azurerm_postgresql_flexible_server.database.id
  start_ip_address = var.postgres_bootstrap_ipv4
  end_ip_address   = var.postgres_bootstrap_ipv4
}
