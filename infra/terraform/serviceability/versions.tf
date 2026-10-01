terraform {
  required_version = ">= 1.1, < 2.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.55, < 5.0"
    }
  }
}
