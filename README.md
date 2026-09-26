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

- physical geographic cell sizing/resolution;
- final clustering/compaction algorithm;
- detailed route-optimization algorithm, if any;
- detailed rider-selection algorithm inside a fleet;
- item category taxonomy and final pricing formula;
- exact payment gateway vendor;
- exact OTP commercial rate/vendor confirmation;
- final CI/CD provider selection: Azure DevOps Pipelines or GitHub Actions;
- frontend/mobile technology;
- final offline-evidence validation policy;
- final retention periods for transactional data, media, logs, inbox/outbox/idempotency records;
- final configurable timing/retry values;
- detailed workflow when actual collected material differs from the booking;
- exact refresh-session retry/rotation/replay semantics, including treatment of a lost successful refresh response.

Azure is the reference MVP cloud, but application/domain code should avoid unnecessary Azure coupling.

## Backend quick start

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
The initial migration only enables PostGIS; this phase intentionally defines no business tables.

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
