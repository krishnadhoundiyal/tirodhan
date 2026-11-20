mock_provider "azurerm" {}

variables {
  resource_group_name  = "tirodhan-np-test01"
  gateway_name         = "tirodhan-np-test01-apim-pilot"
  gateway_principal_id = "11111111-2222-3333-4444-555555555555"
  vault_resource_id    = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/tirodhan-np-test01/providers/Microsoft.KeyVault/vaults/td-np-test01-kv"
  vault_uri            = "https://td-np-test01-kv.vault.azure.net/"
  secret_name          = "existing-gateway-token"
  backend_hostname     = "test-api.example.centralindia.azurecontainerapps.io"
  browser_origins      = ["https://customer.example.invalid", "https://manager.example.invalid"]
  business_operations = {
    current-user = { method = "GET", path = "/auth/me" }
    collection   = { method = "GET", path = "/customer/collection-requests/{id}" }
  }
  web_policy_xml    = "<policies><inbound><set-header name=\"X-Gateway-Secret\" exists-action=\"override\"><value>{{gateway-secret}}</value></set-header></inbound><backend/><outbound/><on-error/></policies>"
  web_policy_sha256 = sha256("<policies><inbound><set-header name=\"X-Gateway-Secret\" exists-action=\"override\"><value>{{gateway-secret}}</value></set-header></inbound><backend/><outbound/><on-error/></policies>")
}

run "create_exact_routes_without_secret_values" {
  command = plan

  assert {
    condition = (
      length(azurerm_api_management_api_operation.web) == 8 &&
      azurerm_api_management_api.web.path == "web/session" &&
      azurerm_api_management_api_operation.web["refresh-options"].url_template == "/refresh" &&
      azurerm_api_management_api_operation.backend["native-refresh"].url_template == "/auth/refresh" &&
      azurerm_api_management_api.backend.service_url == "https://test-api.example.centralindia.azurecontainerapps.io/v1"
    )
    error_message = "The gateway must preserve the distinct browser and native routes."
  }

  assert {
    condition = (
      azurerm_role_assignment.gateway_secret.scope == "${var.vault_resource_id}/secrets/existing-gateway-token" &&
      one(azurerm_api_management_named_value.gateway_secret.value_from_key_vault).secret_id == "https://td-np-test01-kv.vault.azure.net/secrets/existing-gateway-token" &&
      azurerm_api_management_named_value.gateway_secret.secret
    )
    error_message = "APIM must use the reference and its grant must be scoped to the selected secret."
  }
}

run "reject_native_route_on_business_surface" {
  command = plan
  variables {
    business_operations = {
      alternate-refresh = { method = "POST", path = "/auth/refresh" }
    }
  }
  expect_failures = [var.business_operations]
}

run "configure_original_nonprod_gateway" {
  command = plan

  variables {
    gateway_name = "tirodhan-np-test01-apim"
  }

  assert {
    condition = (
      azurerm_api_management_api.web.api_management_name == var.gateway_name &&
      azurerm_api_management_api.backend.api_management_name == var.gateway_name &&
      azurerm_api_management_named_value.gateway_secret.api_management_name == var.gateway_name &&
      azurerm_role_assignment.gateway_secret.principal_id == var.gateway_principal_id
    )
    error_message = "Session configuration must use the original gateway and its own managed identity."
  }
}

run "reject_unreviewed_xml" {
  command = plan
  variables {
    web_policy_sha256 = sha256("another policy")
  }
  expect_failures = [azurerm_api_management_api_policy.web]
}
