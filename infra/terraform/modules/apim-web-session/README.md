# NONPROD APIM web session configuration

This module describes the configuration for an existing Consumption gateway in
the NONPROD resource group. The selected service supplies its system-assigned
principal ID. The module manages its API child resources and one Key Vault
role assignment. Key Vault and FastAPI retain their existing owners.

The protected APIM creation workflow permits only the service create. Register
this module in a reviewed configuration release before applying it. That release
needs a saved plan with the exact gateway, vault scope, API child addresses and
operations, and existing owner approval. Its deployment principal needs the
specific APIM API, operation, policy and named-value actions plus permission to
grant the selected secret scope. The current role for service creation is
insufficient. Validate the teardown inventory and permission profile for any new
module state before admitting the configuration release.

Use the selected gateway's managed identity, the verified vault resource ID and URI, and
the actual FastAPI HTTPS ingress hostname. An authorized NONPROD verifier
confirms that the selected independent secret exists and is enabled. Supply
only its name. The APIM named value is always called `gateway-secret`. FastAPI's
deployed `TIRODHAN_GATEWAY_SECRET` must reference the same Key Vault secret.
Verify both references and live forwarding before activating the guarded API or
publishing a browser login. FastAPI retains its existing RS256 PEM references.
Keep raw secret and PEM contents out of Terraform inputs, state and diagnostics.

Use the approved frontend renderer and its exact NONPROD manifest to supply
`web_policy_xml` and its reviewed SHA-256 as `web_policy_sha256`. The signed
source/configuration review binds the XML, original browser and gateway origins,
backend host, product cookie selection, CSRF marker, route paths, and observed
backend refresh lifetime. The module verifies the digest and a subset of policy
structure. The approved frontend renderer and live APIM acceptance establish the
remaining behavior.

The session API registers these paths for POST and OPTIONS:

- `/web/session/otp/start`
- `/web/session/otp/verify`
- `/web/session/refresh`
- `/web/session/logout`

Supply `business_operations` as an explicit allowlist from the deployed FastAPI
revision. Paths are relative to `/v1`. Include `GET /auth/me` for browser role
admission, and only the customer and manager operations needed for the pilot.
The module declares the native auth paths separately. Its API-level policy
rejects Origin-bearing calls to those operations and leaves native JSON auth
available. The browser session policy is isolated from that forwarding policy
and is responsible for its own CORS, CSRF, cookie handling and output shaping.
Both policies overwrite the invocation header on the upstream hop.

The configuration plan's resource IDs and stable operation keys define the
replay boundary. Apply once under the signed, locked release. After a partial or
uncertain apply, inspect exact Azure IDs and state before generating a fresh
saved plan. The backend owns OTP command idempotency, refresh and logout.

Before browser activation, verify APIM's effective diagnostics and policy
behavior, the FastAPI gateway guard and native regression, both isolated cookie
sessions and error responses, and the exact custom origin and TLS topology. Use
approved synthetic accounts. Keep Cloudflare hostname records for after the web
apps have verified deployment endpoints.

From this directory, `terraform init -backend=false` and `terraform test` run
the module's mock-provider tests. Those tests exercise configuration and plan
validation. They do not call Azure or certify the policy compiler.
