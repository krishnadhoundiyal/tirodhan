terraform {
  backend "azurerm" {
    use_azuread_auth = true
    key              = "tirodhan/nonprod.tfstate"
  }
}
