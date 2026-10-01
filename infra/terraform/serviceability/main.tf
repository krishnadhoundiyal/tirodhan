locals {
  namespace_id = var.existing_service_bus_namespace_id != null ? var.existing_service_bus_namespace_id : azurerm_servicebus_namespace.serviceability[0].id
  worker_secrets = {
    database-url   = var.database_url_secret_uri
    google-api-key = var.google_api_key_secret_uri
    address-keys   = var.address_keys_secret_uri
  }
  common_env = {
    TIRODHAN_ENVIRONMENT                           = var.environment
    TIRODHAN_SERVICE_BUS_NAMESPACE                 = "${var.service_bus_namespace_name}.servicebus.windows.net"
    TIRODHAN_SERVICEABILITY_QUEUE_NAME             = var.queue_name
    TIRODHAN_SERVICE_BUS_OPERATION_TIMEOUT_SECONDS = tostring(var.service_bus_timeout_seconds)
  }
}

resource "azurerm_servicebus_namespace" "serviceability" {
  count               = var.existing_service_bus_namespace_id == null ? 1 : 0
  name                = var.service_bus_namespace_name
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "Standard"
  local_auth_enabled  = false
}

resource "azurerm_servicebus_queue" "serviceability" {
  name                                 = var.queue_name
  namespace_id                         = local.namespace_id
  lock_duration                        = var.queue_lock_duration
  max_delivery_count                   = var.queue_max_delivery_count
  default_message_ttl                  = var.queue_message_ttl
  dead_lettering_on_message_expiration = true
  requires_session                     = false
  requires_duplicate_detection         = false
}

resource "azurerm_user_assigned_identity" "worker" {
  name                = "${var.name_prefix}-serviceability"
  resource_group_name = var.resource_group_name
  location            = var.location
}
resource "azurerm_user_assigned_identity" "publisher" {
  name                = "${var.name_prefix}-outbox"
  resource_group_name = var.resource_group_name
  location            = var.location
}
resource "azurerm_role_assignment" "receiver" {
  scope                = azurerm_servicebus_queue.serviceability.id
  role_definition_name = "Azure Service Bus Data Receiver"
  principal_id         = azurerm_user_assigned_identity.worker.principal_id
}
resource "azurerm_role_assignment" "sender" {
  scope                = azurerm_servicebus_queue.serviceability.id
  role_definition_name = "Azure Service Bus Data Sender"
  principal_id         = azurerm_user_assigned_identity.publisher.principal_id
}
resource "azurerm_role_assignment" "worker_secrets" {
  for_each             = local.worker_secrets
  scope                = "${var.key_vault_id}/secrets/${split("/", each.value)[4]}"
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.worker.principal_id
}
resource "azurerm_role_assignment" "publisher_database_secret" {
  scope                = "${var.key_vault_id}/secrets/${split("/", var.database_url_secret_uri)[4]}"
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.publisher.principal_id
}

resource "azurerm_container_app" "serviceability" {
  name                         = "${var.name_prefix}-serviceability"
  resource_group_name          = var.resource_group_name
  container_app_environment_id = var.container_app_environment_id
  revision_mode                = "Single"
  # No public ingress; reuse the environment's existing observability configuration.
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.worker.id]
  }
  dynamic "secret" {
    for_each = local.worker_secrets
    content {
      name                = secret.key
      key_vault_secret_id = secret.value
      identity            = azurerm_user_assigned_identity.worker.id
    }
  }
  template {
    min_replicas = 0
    max_replicas = var.worker_max_replicas
    container {
      name    = "serviceability"
      image   = var.image
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["python", "-m", "tirodhan.workers.serviceability"]
      dynamic "env" {
        for_each = merge(local.common_env, {
          TIRODHAN_SERVICE_BUS_MANAGED_IDENTITY_CLIENT_ID = azurerm_user_assigned_identity.worker.client_id
          TIRODHAN_SERVICEABILITY_LOCK_RENEWAL_SECONDS    = tostring(var.worker_lock_renewal_seconds)
          TIRODHAN_GOOGLE_MAPS_HTTP_TIMEOUT_SECONDS       = tostring(var.google_timeout_seconds)
          TIRODHAN_GOOGLE_MAPS_DELHI_ADMIN_ALIASES        = jsonencode(var.delhi_admin_aliases)
          TIRODHAN_ADDRESS_ENCRYPTION_ACTIVE_KEY_ID       = var.address_encryption_active_key_id
        })
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name        = "TIRODHAN_DATABASE_URL"
        secret_name = "database-url"
      }
      env {
        name        = "TIRODHAN_GOOGLE_MAPS_API_KEY"
        secret_name = "google-api-key"
      }
      env {
        name        = "TIRODHAN_ADDRESS_ENCRYPTION_KEYS"
        secret_name = "address-keys"
      }
    }
    custom_scale_rule {
      name             = "serviceability-queue"
      custom_rule_type = "azure-servicebus"
      identity_id      = azurerm_user_assigned_identity.worker.id
      metadata = {
        namespace    = var.service_bus_namespace_name
        queueName    = var.queue_name
        messageCount = "1"
      }
    }
  }
  depends_on = [azurerm_role_assignment.receiver, azurerm_role_assignment.worker_secrets]
}

resource "azurerm_container_app_job" "outbox_publisher" {
  name                         = "${var.name_prefix}-outbox"
  location                     = var.location
  resource_group_name          = var.resource_group_name
  container_app_environment_id = var.container_app_environment_id
  replica_timeout_in_seconds   = var.publisher_timeout_seconds
  replica_retry_limit          = 0
  schedule_trigger_config {
    cron_expression          = var.publisher_schedule
    parallelism              = 1
    replica_completion_count = 1
  }
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.publisher.id]
  }
  secret {
    name                = "database-url"
    key_vault_secret_id = var.database_url_secret_uri
    identity            = azurerm_user_assigned_identity.publisher.id
  }
  template {
    container {
      name    = "outbox"
      image   = var.image
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["python", "-m", "tirodhan.workers.outbox_publisher"]
      dynamic "env" {
        for_each = merge(local.common_env, {
          TIRODHAN_SERVICE_BUS_MANAGED_IDENTITY_CLIENT_ID = azurerm_user_assigned_identity.publisher.client_id
          TIRODHAN_OUTBOX_PUBLISH_BATCH_SIZE              = tostring(var.publisher_batch_size)
        })
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name        = "TIRODHAN_DATABASE_URL"
        secret_name = "database-url"
      }
    }
  }
  depends_on = [azurerm_role_assignment.sender, azurerm_role_assignment.publisher_database_secret]
}
