# Only the separately authorized snapshot-backed ownership transfer may apply this forget.
# Foundation rejects the legacy binding until that operation has completed.
removed {
  from = azurerm_key_vault.runtime
  lifecycle {
    destroy = false
  }
}
