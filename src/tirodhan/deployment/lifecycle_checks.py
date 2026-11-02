"""Actual prerequisite evaluators; recovery and state mutation are separate operations."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jwt

from tirodhan.deployment.azure_controls import permission_allows_list_keys
from tirodhan.deployment.check_destroy_plan import validate_destroy_plan
from tirodhan.deployment.check_nonprod_bootstrap_rbac import (
    verify_bootstrap_rbac,
    verify_recovery_definition,
)
from tirodhan.deployment.check_state import populated_key_paths
from tirodhan.deployment.deployment_admission import ROOT, AdmissionDenied, Authorization, digest
from tirodhan.deployment.lifecycle_evidence import (
    BackendState,
    Observations,
    classify_state,
    plan_prior_state,
    state_resources,
)
from tirodhan.deployment.migration_evidence import (
    API_VERSION,
    execution_name,
    migration_template,
    successful_execution,
)
from tirodhan.deployment.prerequisites import CHECK_IDS


class Checks:
    def __init__(
        self,
        authorization: Authorization,
        config: dict[str, Any],
        parameters: dict[str, Any],
        observations: Observations | None = None,
    ) -> None:
        self.auth = authorization
        self.config = config
        self.tf = config["terraform"]
        self.parameters = parameters
        self.io = observations or Observations(config)
        self.observed_state: BackendState | None = None

    def registry(self) -> dict[str, Callable[[], bool]]:
        return {key: getattr(self, "check_" + key) for key in CHECK_IDS}

    def binding(self) -> dict[str, Any]:
        value = self.auth.claims.get("bindings", {}).get(self.auth.operation)
        if not isinstance(value, dict):
            raise AdmissionDenied("OPERATION_BINDING_MISSING")
        return value

    def backend(self) -> BackendState:
        if self.observed_state is None:
            self.observed_state = self.io.backend()
        return self.observed_state

    def check_target(self) -> bool:
        return (
            digest(self.config) == self.auth.configuration_hash
            and self.config["state_resource_group"].casefold()
            != self.tf["resource_group_name"].casefold()
            and re.fullmatch(r"[a-z0-9]{4,10}", self.tf["suffix"]) is not None
            and os.environ.get("DEPLOYMENT_SCOPE", self.io.scope).casefold()
            == self.io.scope.casefold()
        )

    def check_identity(self) -> bool:
        claims = self.io.identity()
        if str(claims.get("oid", "")).casefold() != self.auth.principal_id.casefold():
            return False
        expected = {
            "deployment": self.config["deployment_client_id"],
            "recovery": self.config.get("recovery_client_id"),
            "database_verification": self.config.get("database_verification_client_id"),
        }.get(self.auth.identity_kind)
        return self.auth.identity_kind not in {
            "deployment",
            "recovery",
            "database_verification",
        } or (
            bool(expected)
            and str(claims.get("appid") or claims.get("azp")).casefold() == str(expected).casefold()
        )

    def check_backend(self) -> bool:
        # No parent state-RG read. Absence and uninitialized state remain distinct.
        return self.backend().kind == "INITIALIZED"

    def check_terraform_context(self) -> bool:
        from tirodhan.deployment.backend_setup import terraform_environment_safe

        if not terraform_environment_safe():
            return False
        if self.io.terraform_inputs() is not True:
            return False
        version = self.io.runner.json("terraform", "version", "-json")
        if (
            version.get("terraform_version") != "1.13.5"
            or version.get("provider_selections", {}).get("registry.terraform.io/hashicorp/azurerm")
            != "5.8.0"
        ):
            return False
        backend = self.io.terraform_context()
        config = backend.get("config", {})
        return (
            backend.get("type") == "azurerm"
            and config.get("storage_account_name") == self.config["state_account"]
            and config.get("resource_group_name") == self.config["state_resource_group"]
            and config.get("container_name") == "tfstate"
            and config.get("key") == "tirodhan/nonprod.tfstate"
            and config.get("use_azuread_auth") is True
            and config.get("subscription_id") in (None, "", self.tf["subscription_id"])
            and config.get("tenant_id") in (None, "", self.tf["tenant_id"])
            and config.get("client_id") in (None, "", self.config["deployment_client_id"])
            and not any(config.get(k) for k in ("access_key", "sas_token", "client_secret"))
        )

    def check_execution_authority(self) -> bool:
        binding = self.binding()
        if os.environ.get("GITHUB_ACTIONS") != "true":
            return binding.get("local_execution_authorized") is True
        expected_environment = (
            "nonprod-database"
            if self.auth.operation == "database-verify"
            else "nonprod-recovery"
            if self.auth.operation == "recover-vault"
            else "nonprod-plan"
            if self.auth.operation.endswith("-plan") or self.auth.operation == "rotate-log-sas"
            else "nonprod"
        )
        if self.auth.operation == "terraform-setup":
            setup_environment = binding.get("execution_environment")
            if setup_environment not in {"nonprod", "nonprod-plan"}:
                return False
            expected_environment = str(setup_environment)
        if os.environ.get("GITHUB_REPOSITORY") != "krishnadhoundiyal/tirodhan":
            return False
        if os.environ.get("GITHUB_REF") != "refs/heads/main":
            return False
        if os.environ.get("TIRODHAN_EXECUTION_ENVIRONMENT") != expected_environment:
            return False
        value = self.io.runner.json(
            "gh", "api", f"repos/krishnadhoundiyal/tirodhan/environments/{expected_environment}"
        )
        policy = value.get("deployment_branch_policy") or {}
        protected = policy.get("protected_branches") is True
        if policy.get("custom_branch_policies") is True:
            branches = self.io.runner.json(
                "gh",
                "api",
                f"repos/krishnadhoundiyal/tirodhan/environments/{expected_environment}/deployment-branch-policies",
            )
            patterns = branches.get("branch_policies", [])
            protected = bool(patterns) and all(r.get("name") == "main" for r in patterns)
        reviewers = any(
            r.get("type") == "required_reviewers" and r.get("reviewers")
            for r in value.get("protection_rules", [])
        )
        return protected and (
            expected_environment in {"nonprod-plan", "nonprod-database"} or bool(reviewers)
        )

    def permissions(self, scope: str) -> list[dict[str, Any]]:
        from urllib.parse import urlsplit

        response = self.io.arm(
            scope + "/providers/Microsoft.Authorization/permissions", "2022-04-01"
        )
        if response is None or not isinstance(response.get("value"), list):
            raise AdmissionDenied("PERMISSIONS_UNAVAILABLE")
        values: list[dict[str, Any]] = list(response["value"])
        seen: set[str] = set()
        while response.get("nextLink"):
            link = response["nextLink"]
            url = urlsplit(link)
            if (
                url.scheme != "https"
                or url.netloc != "management.azure.com"
                or url.path.casefold()
                != (scope + "/providers/Microsoft.Authorization/permissions").casefold()
                or link in seen
                or len(seen) >= 100
            ):
                raise AdmissionDenied("PERMISSIONS_PAGINATION_UNVERIFIED")
            seen.add(link)
            response = self.io.runner.json(
                "az", "rest", "--method", "get", "--url", link, "-o", "json"
            )
            if not isinstance(response.get("value"), list):
                raise AdmissionDenied("PERMISSIONS_UNAVAILABLE")
            values.extend(response["value"])
        return values

    def permits(self, scope: str, actions: set[str]) -> bool:
        permissions = self.permissions(scope)
        return all(
            any(
                any(
                    fnmatch.fnmatchcase(action.casefold(), p.casefold())
                    for p in row.get("actions", [])
                )
                and not any(
                    fnmatch.fnmatchcase(action.casefold(), p.casefold())
                    for p in row.get("notActions", [])
                )
                for row in permissions
            )
            for action in actions
        )

    def check_permissions(self) -> bool:
        if any(
            permission_allows_list_keys(self.permissions(scope))
            for scope in (self.io.scope, self.io.state_id)
        ):
            return False
        rows = self.io.runner.json(
            "az",
            "role",
            "assignment",
            "list",
            "--resource-group",
            self.tf["resource_group_name"],
            "--fill-principal-name",
            "false",
            "-o",
            "json",
        )
        if self.binding().get("recovery_principal_id"):
            definitions = self.io.runner.json(
                "az",
                "role",
                "definition",
                "list",
                "--name",
                f"Tirodhan NONPROD Vault Recovery Write {self.tf['suffix']}",
                "--scope",
                self.io.scope,
                "--output",
                "json",
            )
            verify_recovery_definition(definitions, self.io.scope)
        verify_bootstrap_rbac(
            rows,
            expected_scope=self.io.scope,
            expected_principal_id=self.auth.principal_id,
            expected_suffix=self.tf["suffix"],
            recovery_principal_id=self.binding().get("recovery_principal_id"),
            verification_principal_id=self.config.get("database_verification_principal_id") or None,
        )
        return True

    def check_vault(self) -> bool:
        vault = self.io.arm(self.io.vault_id, "2023-07-01")
        if vault is None:
            return False
        props = vault.get("properties", {})
        configured_url = os.environ.get("KEY_VAULT_URL")
        if (
            configured_url
            and configured_url.rstrip("/").casefold()
            != str(props.get("vaultUri", "")).rstrip("/").casefold()
        ):
            return False
        return (
            str(vault.get("id", "")).casefold() == self.io.vault_id.casefold()
            and str(vault.get("location", "")).casefold() == self.tf["location"].casefold()
            and str(props.get("tenantId", "")).casefold() == self.tf["tenant_id"].casefold()
            and props.get("enablePurgeProtection") is True
            and props.get("enableRbacAuthorization") is True
            and props.get("provisioningState") == "Succeeded"
        )

    def check_ownership(self) -> bool:
        state = self.backend()
        if any(
            re.fullmatch(r"(?:.*\.)?azurerm_key_vault\.runtime(?:\[.*\])?", address)
            for address in state.resources
        ):
            return False
        return classify_state(
            state, self.io.inventory(), app_scope=self.io.scope, retained={self.io.vault_id}
        ) not in {"CONFLICT", "BACKEND_ABSENT", "STATE_ABSENT"}

    def check_state(self) -> bool:
        state = self.backend()
        kind = classify_state(
            state, self.io.inventory(), app_scope=self.io.scope, retained={self.io.vault_id}
        )
        if kind not in {"CLEAN_INITIALIZED", "PARTIAL_OWNED", "ESTABLISHED_OWNED"}:
            return False
        outputs = (state.document or {}).get("outputs", {})
        stage = outputs.get("deployment_stage", {}).get("value")
        if self.auth.operation.startswith("foundation-") and stage is not None:
            if type(stage) is not int or stage != 0:
                return False
        if self.auth.operation.startswith(("migration-", "runtime-")):
            if type(stage) is not int or stage not in range(5):
                return False
        return True

    def check_providers(self) -> bool:
        for namespace in (
            "Microsoft.App",
            "Microsoft.Storage",
            "Microsoft.KeyVault",
            "Microsoft.ServiceBus",
            "Microsoft.ManagedIdentity",
            "Microsoft.DBforPostgreSQL",
        ):
            item = self.io.arm(
                f"/subscriptions/{self.tf['subscription_id']}/providers/{namespace}", "2021-04-01"
            )
            if item is None or item.get("registrationState") != "Registered":
                return False
        return True

    def check_postgres_sku(self) -> bool:
        # Ported from PR #84: committed Terraform is the SKU authority, never a fallback SKU.
        source = (ROOT / "infra/terraform/nonprod/database.tf").read_text()
        matches = re.findall(r'^\s*sku_name\s*=\s*"([^"]+)"', source, flags=re.MULTILINE)
        if len(matches) != 1:
            return False
        requested = matches[0].casefold()
        accepted = {requested, requested.split("_", 1)[-1]}
        response = self.io.runner.json(
            "az",
            "postgres",
            "flexible-server",
            "list-skus",
            "--location",
            self.tf["location"],
            "--subscription",
            self.tf["subscription_id"],
            "--output",
            "json",
        )

        def names(value: Any) -> set[str]:
            if isinstance(value, list):
                return set().union(*(names(v) for v in value))
            if isinstance(value, dict):
                found = {
                    str(v).casefold()
                    for k, v in value.items()
                    if k in {"name", "skuName", "sku_name"} and isinstance(v, str)
                }
                return found | set().union(*(names(v) for v in value.values()))
            return set()

        return bool(accepted & names(response))

    def check_features(self) -> bool:
        from tirodhan.deployment.runtime_prerequisites import proposed_settings

        runtime = self.tf.get("runtime_env", {})
        # Validators run against the proposed configuration, never ambient developer secrets.
        settings = proposed_settings(runtime)
        if any(
            (
                settings.customer_fcm_enabled,
                settings.customer_web_push_enabled,
                settings.routing_dispatch_enabled,
                settings.routing_fleet_enabled,
                settings.routing_orchestration_enabled,
                settings.routing_shadow_review_enabled,
            )
        ):
            # These capabilities retain documented external/native/client acceptance gates.
            return False
        return self.tf.get("routing_availability_schedule") is None

    def foundation_ids(self) -> dict[str, str]:
        state = self.io.backend()
        if state.kind != "INITIALIZED":
            raise AdmissionDenied("FOUNDATION_STATE_UNAVAILABLE")
        if any(
            not value.casefold().startswith(self.io.scope.casefold() + "/")
            for value in state.resources.values()
        ):
            raise AdmissionDenied("FOUNDATION_STATE_OUTSIDE_APPROVED_SCOPE")
        return state.resources

    def check_foundation(self) -> bool:
        from tirodhan.deployment.foundation_inventory import required_addresses

        resources = self.foundation_ids()
        required = required_addresses(self.tf)
        if not required.issubset(resources):
            return False
        for address in sorted(required):
            resource_id = resources[address]
            live = self.io.runner.json("az", "resource", "show", "--ids", resource_id, "-o", "json")
            if (
                not isinstance(live, dict)
                or str(live.get("id", "")).casefold() != resource_id.casefold()
            ):
                return False
            status = live.get("properties", {}).get("provisioningState")
            if status is not None and status != "Succeeded":
                return False
        types = {
            "azurerm_postgresql_flexible_server": "2024-08-01",
            "azurerm_user_assigned_identity": "2023-01-31",
            "azurerm_storage_account": "2025-06-01",
            "azurerm_servicebus_namespace": "2024-01-01",
            "azurerm_container_app_environment": "2025-07-01",
        }
        for resource_type, version in types.items():
            matches = [
                value for key, value in resources.items() if key.startswith(resource_type + ".")
            ]
            if not matches:
                return False
            for resource_id in matches:
                live = self.io.arm(resource_id, version)
                if live is None or str(live.get("id", "")).casefold() != resource_id.casefold():
                    return False
                props = live.get("properties", {})
                if resource_type == "azurerm_user_assigned_identity":
                    if not props.get("principalId") or not props.get("clientId"):
                        return False
                elif resource_type == "azurerm_postgresql_flexible_server":
                    if props.get("state") != "Ready" or not props.get("fullyQualifiedDomainName"):
                        return False
                    if props.get("authConfig", {}).get("passwordAuth") != "Disabled":
                        return False
                elif resource_type == "azurerm_servicebus_namespace":
                    if props.get("status") != "Active":
                        return False
                elif props.get("provisioningState") != "Succeeded":
                    return False
        return True

    def database_target(self) -> dict[str, str]:
        resources = self.foundation_ids()
        database = resources.get("azurerm_postgresql_flexible_server.database")
        identity = resources.get("azurerm_user_assigned_identity.runtime")
        if not database or not identity:
            raise AdmissionDenied("DATABASE_TARGET_UNAVAILABLE")
        db = self.io.arm(database, "2024-08-01")
        uami = self.io.arm(identity, "2023-01-31")
        if db is None or uami is None:
            raise AdmissionDenied("DATABASE_TARGET_UNAVAILABLE")
        return {
            "server_id": database,
            "host": db["properties"]["fullyQualifiedDomainName"],
            "principal_id": uami["properties"]["principalId"],
            "role": identity.rsplit("/", 1)[-1],
            "database": "tirodhan",
        }

    def check_database(self) -> bool:
        policy = json.loads((ROOT / "deploy/nonprod-release-policy.json").read_text())
        token = os.environ.get("TIRODHAN_DATABASE_PREREQUISITE_EVIDENCE", "")
        header = jwt.get_unverified_header(token)
        issuer = policy["database_evidence_keys"][header["kid"]]
        facts = jwt.decode(
            token,
            issuer["public_key"],
            algorithms=["EdDSA"],
            issuer="tirodhan-database-bootstrap",
            audience="tirodhan-nonprod-prerequisites",
            options={"require": ["exp", "iat", "sub", "observed_at"]},
        )
        expected = {
            key: True
            for key in (
                "principal_oid",
                "non_admin",
                "postgis",
                "connect",
                "schema_usage",
                "schema_create",
                "spatial_reference_select",
            )
        }
        if facts.get("identity_kind") == "database_verification":
            from tirodhan.deployment.database_evidence import verification_key_is_independent

            if (
                issuer.get("identity_kind") != "database_verification"
                or facts.get("sub") != self.config.get("database_verification_principal_id")
                or not verification_key_is_independent(policy, header["kid"])
            ):
                return False
            expected["verifier_non_admin"] = True
        elif issuer.get("identity_kind") == "database_verification":
            return False
        if os.environ.get("GITHUB_ACTIONS") == "true":
            from tirodhan.deployment.database_verification_request import consumer_context

            if facts.get("identity_kind") != "database_verification" or facts.get(
                "consumer_context"
            ) != consumer_context(self.auth):
                return False
        return (
            facts.get("sub") == issuer["principal_id"]
            and facts.get("configuration_hash") == self.auth.configuration_hash
            and facts.get("contract_hash") == self.auth.contract_hash
            and facts.get("target") == self.database_target()
            and 0 <= time.time() - facts["iat"] <= policy["maximum_evidence_age_seconds"]
            and type(facts["observed_at"]) is int
            and facts["observed_at"] <= facts["iat"]
            and 0 <= time.time() - facts["observed_at"] <= policy["maximum_evidence_age_seconds"]
            and isinstance(facts.get("checks"), dict)
            and set(facts["checks"]) == set(expected)
            and all(v is True for v in facts["checks"].values())
        )

    def check_verification_request(self) -> bool:
        from tirodhan.deployment.database_verification_request import Github, validate_request

        validate_request(self.auth, self.parameters.get("request"), Github())
        return True

    def check_database_verification_authority(self) -> bool:
        from tirodhan.deployment.database_evidence import signer

        signer(self.auth)
        if not self.check_ownership():
            return False
        client = self.config.get("database_verification_client_id")
        principal = self.config.get("database_verification_principal_id")
        for scope in (self.io.scope, self.io.state_id):
            for row in self.permissions(scope):
                if any(
                    not action.casefold().endswith("/read") for action in row.get("actions", [])
                ):
                    return False
                if any(
                    action.casefold()
                    != "microsoft.storage/storageaccounts/blobservices/containers/blobs/read"
                    for action in row.get("dataActions", [])
                ):
                    return False
        return (
            bool(client)
            and bool(principal)
            and str(client).casefold()
            not in {
                str(self.config["deployment_client_id"]).casefold(),
                str(self.config.get("recovery_client_id")).casefold(),
            }
            and principal == self.auth.principal_id
            and str(principal).casefold()
            not in {
                str(self.tf["postgres_admin_object_id"]).casefold(),
                self.database_target()["principal_id"].casefold(),
            }
            and self.binding().get("database_verification_authorized") is True
        )

    def required_secret_names(self) -> set[str]:
        from tirodhan.deployment.release import MIGRATION_SECRET_NAMES

        required = set(MIGRATION_SECRET_NAMES)
        if self.auth.operation.startswith("runtime-"):
            provider = self.tf.get("runtime_env", {}).get("TIRODHAN_OTP_PROVIDER")
            if provider == "2FACTOR":
                required.add("twofactor-api-key")
            elif provider == "KALEYRA_VERIFY":
                required.add("kaleyra-api-key")
            else:
                raise AdmissionDenied("OTP_PROVIDER_NOT_SELECTED")
            required |= {
                "google-maps-api-key",
                "razorpay-key-secret",
                "razorpay-webhook-secret",
                "auth-jwt-private-key-pem",
                "auth-jwt-public-key-pem",
                "phone-encryption-keys",
                "phone-lookup-hmac-key",
                "address-encryption-keys",
                "customer-cursor-signing-key",
                "customer-data-encryption-keys",
                "customer-data-hmac-key",
            }
            if self.tf.get("customer_notifications_schedule") is not None:
                required |= {
                    "customer-cursor-signing-key",
                    "customer-data-encryption-keys",
                    "customer-data-hmac-key",
                }
        return required

    def check_secrets(self) -> bool:
        required = self.required_secret_names()
        rows = self.io.runner.json(
            "az",
            "keyvault",
            "secret",
            "list",
            "--vault-name",
            self.io.vault_id.rsplit("/", 1)[-1],
            "--output",
            "json",
        )
        usable = {
            r["name"]
            for r in rows
            if r.get("attributes", {}).get("enabled") is True
            and (
                r["attributes"].get("expires") is None or self.unexpired(r["attributes"]["expires"])
            )
        }
        if not required.issubset(usable):
            return False
        if self.auth.operation.startswith("runtime-"):
            from tirodhan.deployment.runtime_prerequisites import validate_runtime_material

            material = {}
            for name in required:
                value = self.io.runner.json(
                    "az",
                    "keyvault",
                    "secret",
                    "show",
                    "--vault-name",
                    self.io.vault_id.rsplit("/", 1)[-1],
                    "--name",
                    name,
                    "--output",
                    "json",
                )
                if not isinstance(value.get("value"), str) or not value["value"].strip():
                    return False
                material[name] = value["value"]
            validate_runtime_material(self.tf.get("runtime_env", {}), material)
        return True

    @staticmethod
    def unexpired(value: Any) -> bool:
        from datetime import datetime, timezone

        if isinstance(value, (int, float)):
            return value > time.time()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) > datetime.now(
            timezone.utc
        )

    def check_images(self) -> bool:
        for key in ("APPLICATION_IMAGE", "FLUENT_BIT_IMAGE"):
            image = os.environ.get(key, "")
            if not re.fullmatch(
                r"ghcr\.io/krishnadhoundiyal/[a-z0-9-]+:" + self.auth.commit, image
            ):
                return False
            # Registry access is proven, rather than merely validating the tag syntax.
            self.io.runner.run("docker", "manifest", "inspect", image)
        return True

    def job(self) -> dict[str, Any]:
        from tirodhan.deployment.migration_start import expected_job_id

        resources = self.foundation_ids()
        resource_id = resources.get("azurerm_container_app_job.migration[0]")
        if not resource_id or resource_id.casefold() != expected_job_id(self.config).casefold():
            raise AdmissionDenied("MIGRATION_JOB_MISSING")
        value = self.io.arm(resource_id, API_VERSION)
        if value is None or str(value.get("id", "")).casefold() != resource_id.casefold():
            raise AdmissionDenied("MIGRATION_JOB_MISSING")
        return value

    def check_migration_job(self) -> bool:
        from tirodhan.deployment.migration_start import belongs_to_release, operation_key

        job = self.job()
        image = os.environ.get("APPLICATION_IMAGE", "")
        return migration_template(job, image) and belongs_to_release(
            job, operation_key(self.auth, job["id"], image)
        )

    def check_migration(self) -> bool:
        from tirodhan.deployment.migration_start import belongs_to_release, operation_key

        job = self.job()
        if not migration_template(job, os.environ.get("APPLICATION_IMAGE", "")):
            return False
        execution = self.parameters.get(
            "migration_execution", os.environ.get("TIRODHAN_MIGRATION_EXECUTION")
        )
        if not execution_name(execution):
            return False
        job_id = job["id"]
        value = self.io.arm(job_id + "/executions/" + execution, API_VERSION)
        image = os.environ.get("APPLICATION_IMAGE", "")
        return (
            value is not None
            and successful_execution(value, job_id, execution, image)
            and belongs_to_release(value, operation_key(self.auth, job_id, image))
        )

    def check_saved_plan(self) -> bool:
        path = self.parameters.get("plan")
        if not isinstance(path, Path) or not path.is_file():
            return False
        binding = self.binding()
        automatic_compensation = (
            self.auth.operation == "compensate-foundation"
            and binding.get("automatic_owned_initial_apply_compensation") is True
        )
        if not automatic_compensation and hashlib.sha256(
            path.read_bytes()
        ).hexdigest() != binding.get("plan_sha256"):
            return False
        # Observe again immediately before mutation, even if prior checks used cached state.
        current = self.io.backend()
        expected_state = (
            self.backend().fingerprint
            if automatic_compensation
            else binding.get("state_fingerprint")
        )
        if current.fingerprint != expected_state:
            return False
        plan = self.io.runner.json("terraform", "show", "-json", str(path))
        prior = plan_prior_state(path)
        if current.kind == "INITIALIZED" and (
            prior.get("lineage") != (current.document or {}).get("lineage")
            or prior.get("serial") != (current.document or {}).get("serial")
        ):
            return False
        if current.kind == "STATE_ABSENT" and state_resources(prior):
            return False
        variables = plan.get("variables", {})
        if any(variables.get(k, {}).get("value") != v for k, v in self.tf.items()):
            return False
        if populated_key_paths(plan):
            return False
        changes = [
            r
            for r in plan.get("resource_changes", [])
            if r.get("mode", "managed") == "managed"
            and r.get("change", {}).get("actions") not in ([], ["no-op"])
        ]
        if self.auth.operation in {"migration-apply", "runtime-apply"}:
            from tirodhan.deployment.migration_start import expected_job_id, operation_key

            outputs = (current.document or {}).get("outputs", {})
            runtime = self.auth.operation == "runtime-apply"
            expected_images = {
                "migration_operation_key": operation_key(
                    self.auth, expected_job_id(self.config), os.environ.get("APPLICATION_IMAGE", "")
                ),
                "migration_image": os.environ.get("APPLICATION_IMAGE"),
                "application_image": os.environ.get("APPLICATION_IMAGE")
                if runtime
                else outputs.get("application_image", {}).get("value"),
                "fluent_bit_image": os.environ.get("FLUENT_BIT_IMAGE")
                if runtime
                else outputs.get("fluent_bit_image", {}).get("value")
                or os.environ.get("FLUENT_BIT_IMAGE"),
            }
            if not expected_images["migration_image"] or not expected_images["fluent_bit_image"]:
                return False
            if any(
                variables.get(key, {}).get("value") != value
                for key, value in expected_images.items()
            ):
                return False
        if self.auth.operation == "migration-apply" and any(
            row.get("type") in {"azurerm_container_app", "azurerm_container_app_job"}
            and row.get("address") != "azurerm_container_app_job.migration[0]"
            for row in changes
        ):
            return False
        if self.auth.operation == "runtime-apply":
            from tirodhan.deployment.release import MIGRATION_SECRET_NAMES, RUNTIME_SECRET_NAMES

            names = variables.get("enabled_runtime_secret_names", {}).get("value")
            if (
                not isinstance(names, list)
                or any(not isinstance(name, str) for name in names)
                or len(names) != len(set(names))
                or not set(names).issubset(RUNTIME_SECRET_NAMES)
                or not (self.required_secret_names() - MIGRATION_SECRET_NAMES).issubset(names)
            ):
                return False
        if self.auth.operation == "transfer-vault":
            return len(changes) == 1 and (
                changes[0].get("address") == "azurerm_key_vault.runtime"
                and changes[0].get("change", {}).get("actions") == ["forget"]
                and changes[0].get("change", {}).get("before", {}).get("id", "").casefold()
                == self.io.vault_id.casefold()
            )
        if self.auth.operation == "compensate-foundation":
            validate_destroy_plan(
                plan,
                expected_subscription_id=self.tf["subscription_id"],
                expected_resource_group=self.tf["resource_group_name"],
                protected_state_account=self.config["state_account"],
                protected_state_container="tfstate",
                allow_empty=True,
            )
            if automatic_compensation:
                baseline = self.parameters.get("baseline", {})
                if baseline.get("original_plan_sha256") != binding.get("original_plan_sha256"):
                    return False
                if any(
                    row.get("address") not in baseline.get("created_addresses", [])
                    or row.get("change", {}).get("before", {}).get("id")
                    != current.resources.get(row.get("address"))
                    for row in changes
                ):
                    return False
            return True
        for row in plan.get("resource_changes", []):
            if row.get("mode", "managed") != "managed":
                continue
            if "delete" in row.get("change", {}).get("actions", []):
                return False
            if row.get("type") in {"azurerm_resource_group", "azurerm_key_vault"}:
                return False
            for side in ("before", "after"):
                values = row.get("change", {}).get(side) or {}
                if (
                    values.get("resource_group_name", self.tf["resource_group_name"])
                    != self.tf["resource_group_name"]
                ):
                    return False
                resource_id = values.get("id")
                if resource_id and not resource_id.casefold().startswith(
                    self.io.scope.casefold() + "/"
                ):
                    return False
        variables = plan.get("variables", {})
        expected_stage = (
            0
            if self.auth.operation == "foundation-apply"
            else (
                max(
                    1,
                    (current.document or {})
                    .get("outputs", {})
                    .get("deployment_stage", {})
                    .get("value", 0),
                )
                if self.auth.operation == "migration-apply"
                else 4
            )
        )
        return bool(variables.get("deployment_stage", {}).get("value") == expected_stage)

    def check_bootstrap_authority(self) -> bool:
        aliases = {
            "AZURE_SUBSCRIPTION_ID": self.tf["subscription_id"],
            "AZURE_TENANT_ID": self.tf["tenant_id"],
            "AZURE_LOCATION": self.tf["location"],
            "NONPROD_SUFFIX": self.tf["suffix"],
            "OPERATOR_OBJECT_ID": self.auth.principal_id,
            "DEPLOYMENT_OBJECT_ID": self.binding().get("deployment_principal_id"),
        }
        if (
            self.tf["resource_group_name"] != f"tirodhan-np-{self.tf['suffix']}"
            or self.config["state_resource_group"] != self.tf["resource_group_name"] + "-state"
            or self.config["state_account"] != "tdnpstate" + self.tf["suffix"]
        ):
            return False
        if any(
            not expected or os.environ.get(key) != expected for key, expected in aliases.items()
        ):
            return False
        if self.io.arm(self.io.vault_id, "2023-07-01") is not None:
            if not self.check_vault():
                return False
        else:
            deleted = self.io.runner.json(
                "az",
                "keyvault",
                "list-deleted",
                "--subscription",
                self.tf["subscription_id"],
                "--resource-type",
                "vault",
                "--output",
                "json",
            )
            if not isinstance(deleted, list) or any(
                str(row.get("name", "")).casefold()
                == self.io.vault_id.rsplit("/", 1)[-1].casefold()
                for row in deleted
            ):
                raise AdmissionDenied("SEPARATELY_AUTHORIZED_VAULT_RECOVERY_REQUIRED")
        subscription = "/subscriptions/" + self.tf["subscription_id"]
        if not self.permits(
            subscription,
            {
                "Microsoft.Resources/subscriptions/resourceGroups/write",
                "Microsoft.Resources/subscriptions/providers/register/action",
                "Microsoft.Authorization/roleDefinitions/write",
                "Microsoft.Authorization/roleAssignments/write",
                "Microsoft.Storage/storageAccounts/write",
                "Microsoft.Storage/storageAccounts/blobServices/containers/write",
                "Microsoft.KeyVault/vaults/write",
            },
        ):
            return False
        # bootstrap.sh permits recovery enrollment only when both IDs are present.
        # Never let optional recovery settings bypass D's mandatory identity checks.
        recovery_object = os.environ.get("RECOVERY_OBJECT_ID", "")
        recovery_application = os.environ.get("RECOVERY_FEDERATED_APP_OBJECT_ID", "")
        if bool(recovery_object) != bool(recovery_application):
            return False
        principals = [("DEPLOYMENT_OBJECT_ID", self.config["deployment_client_id"])]
        applications = [("FEDERATED_APP_OBJECT_ID", self.config["deployment_client_id"])]
        if recovery_object:
            recovery_client = self.config.get("recovery_client_id")
            if (
                not recovery_client
                or recovery_client == self.config["deployment_client_id"]
                or recovery_object == os.environ["DEPLOYMENT_OBJECT_ID"]
            ):
                return False
            principals.append(("RECOVERY_OBJECT_ID", recovery_client))
            applications.append(("RECOVERY_FEDERATED_APP_OBJECT_ID", recovery_client))
        for key, expected_client in principals:
            principal = self.io.runner.json("az", "ad", "sp", "show", "--id", os.environ[key])
            if principal.get("id") != os.environ[key] or principal.get("appId") != expected_client:
                return False
        for key, expected_client in applications:
            application = self.io.runner.json("az", "ad", "app", "show", "--id", os.environ[key])
            if application.get("id") != os.environ[key]:
                return False
            owners = self.io.runner.json(
                "az", "ad", "app", "owner", "list", "--id", os.environ[key]
            )
            if not any(row.get("id") == self.auth.principal_id for row in owners):
                return False
            if application.get("appId") != expected_client:
                return False
        return (
            self.binding().get("bootstrap_enrollment_authorized") is True
            and self.binding().get("bootstrap_discovery_authorized") is True
        )

    def check_recovery_authority(self) -> bool:
        allowed = {
            "Microsoft.KeyVault/vaults/read",
            "Microsoft.KeyVault/vaults/write",
            "Microsoft.Authorization/permissions/read",
            "Microsoft.Resources/subscriptions/resourceGroups/read",
            "Microsoft.KeyVault/deletedVaults/read",
            "Microsoft.KeyVault/locations/deletedVaults/read",
            "Microsoft.KeyVault/locations/operationResults/read",
        }
        if any(
            set(row.get("actions", [])) - allowed or row.get("dataActions")
            for row in self.permissions(self.io.scope)
        ):
            return False
        return (
            self.config.get("recovery_client_id") != self.config["deployment_client_id"]
            and self.binding().get("recovery_authorized") is True
            and self.permits(
                self.io.scope, {"Microsoft.KeyVault/vaults/read", "Microsoft.KeyVault/vaults/write"}
            )
        )

    def check_recovery_enrollment_authority(self) -> bool:
        from tirodhan.deployment.recovery_enrollment import verify_application

        if (
            os.environ.get("GITHUB_ACTIONS") == "true"
            or self.binding().get("recovery_enrollment_authorized") is not True
            or self.config.get("recovery_client_id") == self.config["deployment_client_id"]
            or self.tf["resource_group_name"] != f"tirodhan-np-{self.tf['suffix']}"
        ):
            return False
        subscription = "/subscriptions/" + self.tf["subscription_id"]
        if not all(
            self.permits(
                scope,
                {
                    "Microsoft.Authorization/roleDefinitions/write",
                    "Microsoft.Authorization/roleAssignments/write",
                },
            )
            for scope in (subscription, self.io.scope)
        ):
            return False
        verify_application(self)
        return True

    def check_recovery_target(self) -> bool:
        if self.io.arm(self.io.vault_id, "2023-07-01") is not None:
            return self.check_vault()
        rows = self.io.runner.json(
            "az",
            "keyvault",
            "list-deleted",
            "--subscription",
            self.tf["subscription_id"],
            "--resource-type",
            "vault",
            "--output",
            "json",
        )
        matching = [
            r
            for r in rows
            if str(r.get("name", "")).casefold() == self.io.vault_id.rsplit("/", 1)[-1].casefold()
        ]
        return len(matching) == 1 and (
            str(matching[0].get("properties", {}).get("vaultId", "")).casefold()
            == self.io.vault_id.casefold()
            and str(matching[0].get("location", "")).casefold() == self.tf["location"].casefold()
        )

    def check_legacy_ownership(self) -> bool:
        state = self.backend()
        return (
            state.resources.get("azurerm_key_vault.runtime", "").casefold()
            == self.io.vault_id.casefold()
            and len([k for k in state.resources if "azurerm_key_vault.runtime" in k]) == 1
            and self.binding().get("ownership_transfer_authorized") is True
            and state.fingerprint == self.binding().get("state_fingerprint")
        )

    def check_recoverable_backup(self) -> bool:
        backup = self.parameters.get("backup")
        if not isinstance(backup, dict) or not backup.get("snapshot"):
            return False
        expected = self.parameters.get("backup_state")
        return (
            isinstance(expected, dict)
            and digest(expected) == digest(self.backend().document)
            and state_resources(expected) == self.backend().resources
            and backup.get("state_fingerprint") == self.backend().fingerprint
        )

    def check_database_authority(self) -> bool:
        from tirodhan.deployment.database_evidence import signer

        signer(self.auth)  # Signing must be available before any SQL grant is attempted.
        target = self.database_target()
        administrator = self.io.arm(
            target["server_id"] + "/administrators/activeDirectory", "2024-08-01"
        )
        return (
            administrator is not None
            and administrator.get("properties", {}).get("objectId")
            == self.tf["postgres_admin_object_id"]
            and self.binding().get("database_bootstrap_authorized") is True
        )

    def check_secret_batch(self) -> bool:
        from tirodhan.deployment.seed_secrets import approved_secret_batch

        batch = self.parameters.get("batch")
        return (
            isinstance(batch, dict)
            and (approved_secret_batch(set(batch)) or set(batch) == {"ghcr-pull-pat"})
            and all(isinstance(v, str) and bool(v.strip()) for v in batch.values())
        )

    def check_log_storage(self) -> bool:
        state = self.backend().document or {}
        account = state.get("outputs", {}).get("storage_account_name", {}).get("value")
        if account != os.environ.get("LOG_ACCOUNT_NAME"):
            return False
        row = self.io.runner.json(
            "az",
            "storage",
            "container",
            "show",
            "--account-name",
            account,
            "--name",
            "app-logs",
            "--auth-mode",
            "login",
            "--output",
            "json",
        )
        return row.get("properties", {}).get("publicAccess") in (None, "None")

    def check_clean_baseline(self) -> bool:
        baseline = self.parameters.get("baseline")
        if (
            self.binding().get("automatic_owned_initial_apply_compensation") is True
            and not automatic_compensation_context()
        ):
            return False
        stage = (
            (self.backend().document or {})
            .get("outputs", {})
            .get("deployment_stage", {})
            .get("value")
        )
        if stage is not None and (type(stage) is not int or stage != 0):
            return False
        return isinstance(baseline, dict) and (
            baseline.get("classification") == "CLEAN_INITIALIZED"
            and baseline.get("run_id") == self.auth.run_id
            and baseline.get("configuration_hash") == self.auth.configuration_hash
            and baseline.get("contract_hash") == self.auth.contract_hash
            and baseline.get("state_fingerprint") == self.binding().get("clean_start_fingerprint")
            and self.binding().get("compensation_authorized") is True
            and baseline.get("managed_ids") == []
            and baseline.get("disposable_ids") == []
            and baseline.get("original_plan_sha256") == self.binding().get("original_plan_sha256")
            and isinstance(baseline.get("created_addresses"), list)
            and set(self.backend().resources).issubset(set(baseline["created_addresses"]))
        )

    def check_initialization(self) -> bool:
        state = self.backend()
        return (
            state.kind == "STATE_ABSENT"
            and self.binding().get("initialization_authorized") is True
            and classify_state(
                state, self.io.inventory(), app_scope=self.io.scope, retained={self.io.vault_id}
            )
            == "STATE_ABSENT"
        )

    def check_initialization_context(self) -> bool:
        from tirodhan.deployment.backend_setup import initialization_context

        return initialization_context(self.io)

    def check_initialization_intent(self) -> bool:
        from tirodhan.deployment.backend_setup import validate_intent

        validate_intent(self, self.parameters.get("plan"))
        return True


def automatic_compensation_context() -> bool:
    # The trusted foundation workflow holds nonprod-release across apply AND cleanup.
    # A native state lease alone cannot serialize the gap between those operations.
    return os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_WORKFLOW_REF") == (
        "krishnadhoundiyal/tirodhan/.github/workflows/nonprod-foundation.yml@refs/heads/main"
    )
