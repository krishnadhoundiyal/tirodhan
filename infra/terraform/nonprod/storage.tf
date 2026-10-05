resource "azurerm_storage_account" "application" {
  name                            = "tirodhannp${var.suffix}"
  resource_group_name             = data.azurerm_resource_group.nonprod.name
  location                        = var.location
  account_tier                    = "Standard"
  account_kind                    = "StorageV2"
  account_replication_type        = "LRS"
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  blob_properties {
    versioning_enabled = false
    delete_retention_policy { days = var.blob_soft_delete_days }
    container_delete_retention_policy { days = var.blob_soft_delete_days }
  }
  tags = local.tags
}
resource "azurerm_storage_container" "private" {
  for_each              = toset(["media", "app-logs"])
  name                  = each.value
  storage_account_id    = azurerm_storage_account.application.id
  container_access_type = "private"
}
resource "azurerm_storage_management_policy" "lifecycle" {
  storage_account_id = azurerm_storage_account.application.id
  rule {
    name    = "media"
    enabled = true
    filters {
      prefix_match = ["media/"]
      blob_types   = ["blockBlob"]
    }
    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than = var.media_cool_after_days
        delete_after_days_since_modification_greater_than       = var.media_delete_after_days
      }
    }
  }
  rule {
    name    = "logs"
    enabled = true
    filters {
      prefix_match = ["app-logs/"]
      blob_types   = ["blockBlob"]
    }
    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = var.logs_delete_after_days
      }
    }
  }
}
