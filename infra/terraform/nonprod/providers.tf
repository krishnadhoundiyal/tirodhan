provider "azurerm" {
  features {
    api_management {
      purge_soft_delete_on_destroy = false
      recover_soft_deleted         = false
    }
    key_vault {
      purge_soft_delete_on_destroy = false
    }
  }
  subscription_id                 = var.subscription_id
  tenant_id                       = var.tenant_id
  storage_use_azuread             = true
  resource_provider_registrations = "none"
}
