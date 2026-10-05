resource "azurerm_container_app" "api" {
  count                        = var.deployment_stage >= 2 ? 1 : 0
  name                         = "${local.prefix}-api"
  resource_group_name          = data.azurerm_resource_group.nonprod.name
  container_app_environment_id = azurerm_container_app_environment.nonprod.id
  workload_profile_name        = "Consumption"
  revision_mode                = "Single"
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

  ingress {
    external_enabled           = true
    allow_insecure_connections = false
    target_port                = 8000
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
  template {
    min_replicas = 0
    max_replicas = 2
    http_scale_rule {
      name                = "http"
      concurrent_requests = 10
    }
    container {
      name   = "application"
      image  = var.application_image
      cpu    = 0.5
      memory = "1Gi"
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

      volume_mounts {
        name = "logs"
        path = local.shared_dir
      }
      startup_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/health"
      }
      liveness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/health"
      }
      readiness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/ready"
      }
    }
    container {
      name   = "fluent-bit"
      image  = var.fluent_bit_image
      cpu    = 0.25
      memory = "0.5Gi"
      args   = ["service"]
      volume_mounts {
        name = "logs"
        path = local.shared_dir
      }
      dynamic "env" {
        for_each = merge(local.sidecar_env, { LOG_WORKLOAD = "api" })
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
  depends_on = [azurerm_role_assignment.secrets, azurerm_role_assignment.media, azurerm_role_assignment.delegation]
  tags       = local.tags
}
resource "azurerm_container_app" "worker" {
  for_each                     = var.deployment_stage >= 3 ? local.workers : {}
  name                         = "${local.prefix}-${each.key}"
  resource_group_name          = data.azurerm_resource_group.nonprod.name
  container_app_environment_id = azurerm_container_app_environment.nonprod.id
  workload_profile_name        = "Consumption"
  revision_mode                = "Single"
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
    min_replicas = 0
    max_replicas = each.value.max
    # Azure Container Apps Service Bus scale rule, not separately managed infrastructure.
    custom_scale_rule {
      name             = "service-bus"
      custom_rule_type = "azure-servicebus"
      identity_id      = azurerm_user_assigned_identity.runtime.id
      metadata = {
        namespace    = azurerm_servicebus_namespace.bus.name
        queueName    = local.queues[each.key]
        messageCount = "1"
      }
    }
    container {
      name    = "application"
      image   = var.application_image
      cpu     = each.value.cpu
      memory  = each.value.memory
      command = ["python", "-m", "tirodhan.deployment.entrypoint"]
      args    = ["python", "-m", "tirodhan.workers.${each.value.module}"]
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
      args   = ["service"]
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
  depends_on = [azurerm_container_app.api, azurerm_role_assignment.secrets, azurerm_role_assignment.bus_sender, azurerm_role_assignment.bus_receiver]
  tags       = local.tags
}
