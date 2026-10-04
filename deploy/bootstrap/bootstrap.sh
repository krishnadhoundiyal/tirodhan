#!/usr/bin/env bash
# Run once/repeatedly as an authorized operator, not the restricted deploy identity.
set -euo pipefail
: "${AZURE_SUBSCRIPTION_ID:?}" "${AZURE_TENANT_ID:?}" "${AZURE_LOCATION:?}"
: "${NONPROD_SUFFIX:?}" "${DEPLOYMENT_OBJECT_ID:?}" "${OPERATOR_OBJECT_ID:?}"
: "${FEDERATED_APP_OBJECT_ID:?}" "${GITHUB_REPOSITORY:?}"
[[ "$NONPROD_SUFFIX" =~ ^[a-z0-9]{4,10}$ ]] || exit 2
[[ "$GITHUB_REPOSITORY" == "krishnadhoundiyal/tirodhan" ]] || exit 2
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
[[ "$(az account show --query tenantId -o tsv)" == "$AZURE_TENANT_ID" ]] || exit 2
# Stable role-assignment GUIDs make repeated bootstrap calls idempotent.
grant_role() {
  local role="$1" target="$2" principal="$3" kind="$4"
  local assignment
  assignment="$(python3 -c 'import sys,uuid; print(uuid.uuid5(uuid.NAMESPACE_URL,"/".join(sys.argv[1:])))' "$role" "$target" "$principal")"
  local args=(--name "$assignment" --assignee-object-id "$principal" --assignee-principal-type "$kind" --role "$role" --scope "$target" --output none)
  if [[ -n "${5:-}" ]]; then args+=(--condition-version 2.0 --condition "$5"); fi
  az role assignment create "${args[@]}"
}
scope="/subscriptions/$AZURE_SUBSCRIPTION_ID"
rg="tirodhan-np-$NONPROD_SUFFIX"
state_rg="$rg-state"
state_account="tdnpstate$NONPROD_SUFFIX"
for provider in Microsoft.App Microsoft.Storage Microsoft.KeyVault Microsoft.ServiceBus Microsoft.ManagedIdentity Microsoft.DBforPostgreSQL; do
  az provider register --namespace "$provider" --wait --output none
done
az group create -n "$rg" -l "$AZURE_LOCATION" --output none
az group create -n "$state_rg" -l "$AZURE_LOCATION" --output none
az storage account create -n "$state_account" -g "$state_rg" -l "$AZURE_LOCATION" \
  --sku Standard_LRS --kind StorageV2 --allow-shared-key-access false \
  --allow-blob-public-access false --default-to-oauth-authentication true \
  --min-tls-version TLS1_2 --https-only true --output none
state_id="$scope/resourceGroups/$state_rg/providers/Microsoft.Storage/storageAccounts/$state_account"
rg_scope="$scope/resourceGroups/$rg"
grant_role "Storage Blob Data Contributor" "$state_id" "$OPERATOR_OBJECT_ID" User
# RBAC propagation is asynchronous. Bounded retries, no key-auth fallback.
created=false
for attempt in {1..12}; do
  if az storage container create -n tfstate --account-name "$state_account" --auth-mode login --output none 2>/dev/null; then
    created=true; break
  fi
  sleep 10
done
[[ "$created" == true ]] || { echo "State data-plane RBAC not ready"; exit 1; }
temp_dir="$(mktemp -d)"
trap 'rm -f "$temp_dir/role.json" "$temp_dir/federation.json"; rmdir "$temp_dir"' EXIT
jq --arg scope "$rg_scope" --arg name "Tirodhan NONPROD Deployment $NONPROD_SUFFIX" \
  '.AssignableScopes=[$scope] | .Name=$name' deploy/bootstrap/deployment-role.json > "$temp_dir/role.json"
if az role definition list --name "Tirodhan NONPROD Deployment $NONPROD_SUFFIX" --query '[0].id' -o tsv | grep -q .; then
  az role definition update --role-definition "$temp_dir/role.json" --output none
else
  az role definition create --role-definition "$temp_dir/role.json" --output none
fi
grant_role "Tirodhan NONPROD Deployment $NONPROD_SUFFIX" "$rg_scope" "$DEPLOYMENT_OBJECT_ID" ServicePrincipal
for target in "$state_id" "$rg_scope"; do
  grant_role "Storage Blob Data Contributor" "$target" "$DEPLOYMENT_OBJECT_ID" ServicePrincipal
done
grant_role Reader "$state_id" "$DEPLOYMENT_OBJECT_ID" ServicePrincipal
grant_role "Storage Blob Delegator" "$rg_scope" "$DEPLOYMENT_OBJECT_ID" ServicePrincipal
grant_role "Key Vault Secrets Officer" "$rg_scope" "$DEPLOYMENT_OBJECT_ID" ServicePrincipal
# Deployment may manage only these runtime data roles, never Owner/Contributor.
roles="{69a216fc-b8fb-44d8-bc22-1f3c2cd27a39, 4f6db5ce-55e8-4d65-b4d2-4d2aade53608, ba92f5b4-2d11-453d-a403-e96b0029c9fe, db58b8e5-c6ad-4a2a-8342-4190687cbf4a, 4633458b-17de-408a-b874-0445c86b69e6}"
condition="((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals $roles)) AND ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals $roles))"
grant_role "Role Based Access Control Administrator" "$rg_scope" "$DEPLOYMENT_OBJECT_ID" ServicePrincipal "$condition"
for environment in nonprod-plan nonprod; do
  jq -n --arg name "tirodhan-$environment" --arg subject "repo:$GITHUB_REPOSITORY:environment:$environment" \
    '{name:$name,issuer:"https://token.actions.githubusercontent.com",subject:$subject,audiences:["api://AzureADTokenExchange"]}' > "$temp_dir/federation.json"
  if ! az ad app federated-credential list --id "$FEDERATED_APP_OBJECT_ID" --query "[?name=='tirodhan-$environment'].name" -o tsv | grep -q .; then
    az ad app federated-credential create --id "$FEDERATED_APP_OBJECT_ID" --parameters "$temp_dir/federation.json" --output none
  fi
done
echo "Bootstrap complete: backend account=$state_account container=tfstate; application RG=$rg"
echo "Verify inherited permissions; this script cannot remove pre-existing broad grants."
