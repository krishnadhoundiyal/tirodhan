# Bootstrap executes only on approved migration applies; no scheduler or ingress.
# The dedicated MI is a PostgreSQL Entra administrator, but it has no application
# Service Bus, media Blob, or broad application Key Vault roles.
resource "azurerm_role_assignment" "postgres_bootstrap_secrets" {
  for_each             = var.deployment_stage >= 1 ? toset(["ghcr-pull-pat", "fluent-bit-sas"]) : toset([])
  scope                = "${azurerm_key_vault.runtime.id}/secrets/${each.value}"
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.postgres_bootstrap.principal_id
}

resource "azurerm_container_app_job" "postgres_bootstrap" {
  count                        = var.deployment_stage >= 1 ? 1 : 0
  name                         = "${local.prefix}-pgboot"
  resource_group_name          = data.azurerm_resource_group.nonprod.name
  location                     = var.location
  container_app_environment_id = azurerm_container_app_environment.nonprod.id
  workload_profile_name        = "Consumption"
  replica_timeout_in_seconds   = 900
  replica_retry_limit          = 0

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.postgres_bootstrap.id]
  }

  registry {
    server               = "ghcr.io"
    username             = var.ghcr_username
    password_secret_name = "ghcr-pull-pat"
  }

  dynamic "secret" {
    for_each = local.migration_secret_refs
    content {
      name                = secret.key
      key_vault_secret_id = secret.value
      identity            = azurerm_user_assigned_identity.postgres_bootstrap.id
    }
  }

  template {
    container {
      name    = "application"
      image   = var.migration_image
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["python", "-m", "tirodhan.deployment.entrypoint"]
      args    = ["python", "-m", "tirodhan.deployment.job", "--", "python", "-m", "tirodhan.deployment.bootstrap_database"]

      env {
        name  = "POSTGRES_HOST"
        value = azurerm_postgresql_flexible_server.database.fqdn
      }
      env {
        name  = "POSTGRES_ADMIN_NAME"
        value = azurerm_user_assigned_identity.postgres_bootstrap.name
      }
      env {
        name  = "POSTGRES_BOOTSTRAP_CLIENT_ID"
        value = azurerm_user_assigned_identity.postgres_bootstrap.client_id
      }
      env {
        name  = "RUNTIME_DATABASE_ROLE"
        value = azurerm_user_assigned_identity.runtime.name
      }
      env {
        name  = "RUNTIME_PRINCIPAL_ID"
        value = azurerm_user_assigned_identity.runtime.principal_id
      }
      env {
        name  = "TIRODHAN_LOG_FILE_PATH"
        value = "${local.shared_dir}/application.jsonl"
      }
      env {
        name  = "LOG_SHARED_DIR"
        value = local.shared_dir
      }
      volume_mounts {
        name = "logs"
        path = local.shared_dir
      }
    }

    container {
      name   = "fluent-bit"
      image  = var.fluent_bit_image
      cpu    = 0.25
      memory = "0.5Gi"
      args   = ["job"]
      volume_mounts {
        name = "logs"
        path = local.shared_dir
      }
      dynamic "env" {
        for_each = merge(local.sidecar_env, { LOG_WORKLOAD = "postgres-bootstrap" })
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name  = "JOB_STARTUP_SECONDS"
        value = "300"
      }
      env {
        name        = "LOG_SAS"
        secret_name = "fluent-bit-sas"
      }
    }

    volume {
      name         = "logs"
      storage_type = "EmptyDir"
    }
  }

  lifecycle {
    precondition {
      condition     = var.migration_image != null && var.fluent_bit_image != null
      error_message = "Publish bootstrap application and logging images before stage 1."
    }
  }

  depends_on = [
    azurerm_postgresql_flexible_server_active_directory_administrator.bootstrap,
    azurerm_postgresql_flexible_server_configuration.extensions,
    azurerm_role_assignment.postgres_bootstrap_secrets,
  ]
  tags       = local.tags
}
