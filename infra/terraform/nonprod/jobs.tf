resource "azurerm_container_app_job" "migration" {
  count                        = var.deployment_stage >= 1 ? 1 : 0
  name                         = "${local.prefix}-migrate"
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
    identity_ids = [azurerm_user_assigned_identity.runtime.id]
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
      identity            = azurerm_user_assigned_identity.runtime.id
    }
  }

  template {
    container {
      name    = "application"
      image   = var.migration_image
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["python", "-m", "tirodhan.deployment.entrypoint"]
      args    = ["python", "-m", "tirodhan.deployment.job", "--", "alembic", "upgrade", "head"]
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
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
        for_each = merge(local.sidecar_env, { LOG_WORKLOAD = "migration" })
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
      error_message = "Publish migration and logging images before stage 1."
    }
  }
  depends_on = [azurerm_role_assignment.secrets, azurerm_postgresql_flexible_server_configuration.extensions, azurerm_postgresql_flexible_server_active_directory_administrator.administrator]
  tags       = local.tags
}
resource "azurerm_container_app_job" "scheduled" {
  for_each                     = var.deployment_stage >= 4 ? local.jobs : {}
  name                         = "${local.prefix}-${replace(each.key, "_", "-")}"
  resource_group_name          = data.azurerm_resource_group.nonprod.name
  location                     = var.location
  container_app_environment_id = azurerm_container_app_environment.nonprod.id
  workload_profile_name        = "Consumption"
  replica_timeout_in_seconds   = 300
  replica_retry_limit          = 0
  schedule_trigger_config {
    cron_expression          = var.job_schedules[each.key]
    parallelism              = 1
    replica_completion_count = 1
  }
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.runtime.id]
  }
  registry {
    server               = "ghcr.io"
    username             = var.ghcr_username
    password_secret_name = "ghcr-pull-pat"
  }
  dynamic "secret" {
    for_each = local.secret_refs
    content {
      name                = secret.key
      key_vault_secret_id = secret.value
      identity            = azurerm_user_assigned_identity.runtime.id
    }
  }

  template {
    container {
      name    = "application"
      image   = var.application_image
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["python", "-m", "tirodhan.deployment.entrypoint"]
      args    = ["python", "-m", "tirodhan.deployment.job", "--", "python", "-m", "tirodhan.workers.${each.value}"]
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.runtime_secret_env
        content {
          name        = env.key
          secret_name = env.value
        }
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
        for_each = merge(local.sidecar_env, { LOG_WORKLOAD = each.key })
        content {
          name  = env.key
          value = env.value
        }
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
      condition     = var.application_image != null && var.fluent_bit_image != null
      error_message = "Publish both SHA-tagged images before enabling workloads."
    }
  }
  depends_on = [azurerm_container_app.worker]
  tags       = local.tags
}
