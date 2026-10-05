locals {
  prefix = "tirodhan-np-${var.suffix}"
  tags   = { application = "tirodhan", environment = "nonprod" }
  workers = {
    serviceability = { module = "serviceability", cpu = 0.25, memory = "0.5Gi", max = 2 }
    rider          = { module = "rider_notifications", cpu = 0.25, memory = "0.5Gi", max = 2 }
    refund         = { module = "refunds", cpu = 0.25, memory = "0.5Gi", max = 1 }
    planning       = { module = "planning_worker", cpu = 0.5, memory = "1Gi", max = 2 }
  }
  queues = {
    serviceability = "serviceability"
    rider          = "rider-notification"
    refund         = "refund"
    planning       = "planning"
  }
  jobs = {
    outbox                 = "outbox_publisher"
    planning_scheduler     = "planning_scheduler"
    fleet_timeout          = "fleet_timeout"
    pending_payment_expiry = "pending_payment_expiry"
  }
  # Paths are deployment choices, not domain configuration.
  shared_dir = "/var/log/tirodhan"
  common_env = merge(var.runtime_env, {
    TIRODHAN_ENVIRONMENT                            = "nonprod"
    TIRODHAN_DEBUG                                  = "false"
    TIRODHAN_DATABASE_ECHO                          = "false"
    TIRODHAN_LOG_FILE_PATH                          = "${local.shared_dir}/application.jsonl"
    TIRODHAN_DATABASE_URL                           = "postgresql+asyncpg://${azurerm_user_assigned_identity.runtime.name}@${azurerm_postgresql_flexible_server.database.fqdn}:5432/tirodhan"
    TIRODHAN_DATABASE_ENTRA_AUTHENTICATION          = "true"
    TIRODHAN_DATABASE_MANAGED_IDENTITY_CLIENT_ID    = azurerm_user_assigned_identity.runtime.client_id
    AZURE_CLIENT_ID                                 = azurerm_user_assigned_identity.runtime.client_id
    TIRODHAN_SERVICE_BUS_MANAGED_IDENTITY_CLIENT_ID = azurerm_user_assigned_identity.runtime.client_id
    TIRODHAN_SERVICE_BUS_NAMESPACE                  = "${azurerm_servicebus_namespace.bus.name}.servicebus.windows.net"
    TIRODHAN_SERVICEABILITY_QUEUE_NAME              = local.queues.serviceability
    TIRODHAN_RIDER_NOTIFICATION_QUEUE_NAME          = local.queues.rider
    TIRODHAN_REFUND_QUEUE_NAME                      = local.queues.refund
    TIRODHAN_PLANNING_QUEUE_NAME                    = local.queues.planning
    TIRODHAN_MEDIA_BLOB_ACCOUNT_URL                 = azurerm_storage_account.application.primary_blob_endpoint
    TIRODHAN_MEDIA_BLOB_CONTAINER_NAME              = "media"
  })
  secret_refs = merge(
    { for env, name in var.runtime_secret_names : name => "${azurerm_key_vault.runtime.vault_uri}secrets/${name}" },
    {
      ghcr-pull-pat  = "${azurerm_key_vault.runtime.vault_uri}secrets/ghcr-pull-pat"
      fluent-bit-sas = "${azurerm_key_vault.runtime.vault_uri}secrets/fluent-bit-sas"
    }
  )
  sidecar_env = {
    LOG_ACCOUNT_NAME = azurerm_storage_account.application.name
    LOG_SHARED_DIR   = local.shared_dir
    LOG_WORKLOAD     = "nonprod"
  }
}
