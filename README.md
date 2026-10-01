# Tirodhan

This repository contains the Tirodhan collection platform. Phase 1A provides the
transactional backend foundation: a modular FastAPI application, PostgreSQL/PostGIS
connectivity, Alembic migrations, containerized local development, and test tooling.

## Read first

For every task:

1. `AGENTS.md`
2. `docs/PROJECT_CONTEXT.md`
3. `docs/ARCHITECTURE.md`

Then read only the detailed domain, schema, idempotency, data-protection and ADR documents relevant to the task. `AGENTS.md` defines the routing rules. If applicability is uncertain, read the document rather than guessing.

## Current architecture status

The backend/cloud architecture, core domain boundaries, idempotency model, and initial PostgreSQL/PostGIS schema design have been substantially defined for the MVP.

The following remain intentionally open and must not be silently decided by an agent:

- final clustering/compaction algorithm;
- detailed route-optimization algorithm, if any;
- detailed rider-selection algorithm inside a fleet;
- item category taxonomy and final pricing formula;
- exact payment gateway vendor;
- exact OTP commercial rate (Kaleyra Verify is the Phase 1R runtime provider);
- final CI/CD provider selection: Azure DevOps Pipelines or GitHub Actions;
- frontend/mobile technology;
- final offline-evidence validation policy;
- final retention periods for transactional data, media, logs, inbox/outbox/idempotency records;
- final configurable timing/retry values;
- detailed workflow when actual collected material differs from the booking;
- exact refresh-session retry/rotation/replay semantics, including treatment of a lost successful refresh response.

Azure is the reference MVP cloud, but application/domain code should avoid unnecessary Azure coupling.

## Backend quick start

Phase 1T freezes Google Geocoding v3 and H3 resolution 7 (raw canonical cell IDs),
with asynchronous Service Bus Standard serviceability as primary and synchronous
checkout fallback using the same resolution operation. See ADR-015. Google and broker
I/O hold no PostgreSQL transaction. Exact publisher/consumer hosting, scheduling and
scaling topology is deferred; the process entry points do not freeze ACA Job/worker choices.
Unvalidated Phase 1T Terraform artifacts have been removed.

Runtime entry points (same backend image, separate processes):

```bash
python -m tirodhan.workers.serviceability
python -m tirodhan.workers.outbox_publisher
```

Configure the Google API key, bounded HTTP timeout and Delhi aliases listed in
`.env.example` for both API fallback and worker. The worker also requires address
encryption configuration. Broker settings are the fully qualified namespace, dedicated
queue name, managed-identity client ID, bounded operation timeout and worker lock-renewal
duration. The finite publisher additionally requires an explicit batch size. Neither
process accepts a Service Bus connection string. Missing Google configuration leaves
API fallback unavailable (503); it never substitutes a location. No test calls live
Google or Azure Service Bus. Hosted topology and IaC require later deployment review;
see [ADR-015](docs/adr/ADR-015-serviceability-location-runtime.md).

The host workflow requires Python 3.10 or newer. The container image uses Python 3.12.

```bash
python -m venv .venv
# PowerShell: .venv\Scripts\Activate.ps1
# bash/zsh: source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env`, start PostGIS, apply migrations, then run the API:

```bash
docker compose up -d database
alembic upgrade head
uvicorn tirodhan.main:app --reload --no-access-log
```

Alternatively, start the full local stack. Compose waits for Postgres, runs the migration
job once, and then starts the API:

```bash
docker compose up --build
```

The local endpoints are:

- `GET http://localhost:8000/health` — process liveness, independent of the database;
- `GET http://localhost:8000/ready` — PostgreSQL connectivity and PostGIS readiness;
- `GET http://localhost:8000/docs` — generated OpenAPI documentation.

JSON logs go to the console by default. Hosted environments can set
`TIRODHAN_LOG_FILE_PATH` to a file on their mounted shared replica-local volume; the
application does not choose or create the Azure volume mount.

Local credentials in `.env.example` and `compose.yaml` are intentionally local-only. Runtime
secrets for hosted environments must come from their environment/secret provider and must not
be committed.

### OTP runtime (Phase 1R)

Configure the Kaleyra HTTPS origin, SID, API key, Verify flow and bounded timeout, plus the
local challenge TTL, using the settings listed in `.env.example`. Missing provider settings
fail closed (503); there is no fallback provider. No tests contact live Kaleyra.

Phone protection requires an active encryption key ID, a JSON keyring of standard-base64
32-byte AES keys and an independent standard-base64 32-byte HMAC key. Retain old encryption
key IDs for decryption. Lookup-key rotation requires an explicit maintenance/data migration.
Supply production secrets via Key Vault -> ACA secret/reference -> process configuration.

`POST /v1/auth/otp/start` accepts `client_request_id` and canonical E.164 `phone`, returning a
public UUIDv7 `challenge_reference`. `POST /v1/auth/otp/verify` accepts `client_login_id`, that
reference and `code`; phone is forbidden. Provider references remain private. A new OTP needs
a new start key, not a resend call. A consumed, superseded or locally expired start key conflicts.
Start reserves `auth.start` before Generate: only one same-key caller can invoke the provider;
IN_PROGRESS returns 409, while completed live replay returns the same challenge. Ambiguous provider
or post-invocation local failures keep the key reserved and require a genuinely new start key.
Successful login consumes the local challenge atomically with identity/session persistence.
Provider success followed by local failure requires a new start; completed login replay cannot
reconstruct credentials. Refresh, logout and downstream authorization are unchanged.

## Quality checks

```bash
ruff check .
ruff format --check .
mypy
pytest
```

Integration tests are skipped unless a disposable PostGIS database is explicitly supplied:

```bash
TIRODHAN_TEST_DATABASE_URL=postgresql+asyncpg://tirodhan:tirodhan@localhost:5432/tirodhan pytest -m integration
```

## Migrations

Alembic reads `TIRODHAN_DATABASE_URL` through the same typed settings object as the application.
Migrations currently provide PostGIS, the identity/reliability substrate, saved addresses, and
serviceability contexts.

```bash
alembic current
alembic upgrade head
alembic downgrade base
```

Application startup never runs migrations implicitly. The local Compose migration job is an
explicit convenience; hosted deployment orchestration remains responsible for migrations.

## Backend layout

```text
src/tirodhan/
├── api/                 # HTTP composition and platform routes
├── core/                # typed settings and structured logging
├── db/                  # SQLAlchemy metadata, engine, and sessions
├── modules/             # approved modular-monolith ownership boundaries
│   ├── identity/
│   ├── customers/
│   ├── serviceability/
│   ├── collection_requests/
│   ├── payments/
│   ├── planning/
│   ├── riders/
│   ├── assignments/
│   ├── pickups/
│   ├── operations/
│   ├── receiving_points/
│   ├── evidence/
│   └── reliability/
└── main.py              # application factory and ASGI entry point

migrations/              # Alembic environment and revisions
tests/unit/              # isolated application tests
tests/integration/       # opt-in disposable-database tests
```

The module packages are boundaries only. Business entities and workflows are deliberately out
of scope for Phase 1A.
