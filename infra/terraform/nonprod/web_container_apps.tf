locals {
  web_images = var.deployment_stage >= 4 && var.nonprod_web_images != null ? tomap(var.nonprod_web_images) : tomap({})
}

resource "azurerm_user_assigned_identity" "web_pull" {
  count               = length(local.web_images) == 0 ? 0 : 1
  name                = "${local.prefix}-web-pull"
  resource_group_name = data.azurerm_resource_group.nonprod.name
  location            = var.location
  tags                = local.tags
}

resource "azurerm_role_assignment" "web_pull" {
  count                = length(local.web_images) == 0 ? 0 : 1
  scope                = "${data.azurerm_key_vault.runtime.id}/secrets/ghcr-pull-pat"
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.web_pull[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_container_app" "web" {
  for_each                     = local.web_images
  name                         = "${local.prefix}-${each.key}-web"
  resource_group_name          = data.azurerm_resource_group.nonprod.name
  container_app_environment_id = azurerm_container_app_environment.nonprod.id
  workload_profile_name        = "Consumption"
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.web_pull[0].id]
  }

  secret {
    name                = "ghcr-pull-pat"
    key_vault_secret_id = local.migration_secret_refs["ghcr-pull-pat"]
    identity            = azurerm_user_assigned_identity.web_pull[0].id
  }

  registry {
    server               = "ghcr.io"
    username             = var.ghcr_username
    password_secret_name = "ghcr-pull-pat"
  }

  ingress {
    external_enabled           = true
    allow_insecure_connections = false
    target_port                = 8080
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
      concurrent_requests = 20
    }
    container {
      name    = "web"
      image   = each.value
      cpu     = 0.25
      memory  = "0.5Gi"
      args    = []
      command = []

      dynamic "env" {
        for_each = {
          WEB_ENVIRONMENT         = "nonprod"
          WEB_AUTH_ENABLED        = "false"
          WEB_AUTH_BROWSER_ORIGIN = "https://${each.key}-nonprod.qtkservice.com"
          WEB_AUTH_GATEWAY_ORIGIN = "https://api-nonprod.qtkservice.com"
          WEB_AUTH_START_PATH     = "/web/session/otp/start"
          WEB_AUTH_VERIFY_PATH    = "/web/session/otp/verify"
          WEB_AUTH_REFRESH_PATH   = "/web/session/refresh"
          WEB_AUTH_LOGOUT_PATH    = "/web/session/logout"
          WEB_AUTH_CSRF_HEADER    = "X-Tirodhan-Web-Session"
          WEB_AUTH_CSRF_VALUE     = "1"
        }
        content {
          name  = env.key
          value = env.value
        }
      }

      startup_probe {
        transport = "HTTP"
        port      = 8080
        path      = "/health"
      }
      readiness_probe {
        transport = "HTTP"
        port      = 8080
        path      = "/health"
      }
      liveness_probe {
        transport = "HTTP"
        port      = 8080
        path      = "/health"
      }
    }
  }

  depends_on = [azurerm_role_assignment.web_pull]
}
