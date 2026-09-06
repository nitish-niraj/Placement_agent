# Placement Intelligence Agent (PIA)

A private, personal AI agent for a single student: it reads selected WhatsApp
placement/academic groups through a self-hosted **Evolution API** connector, detects
company eligibility lists (Excel/CSV/PDF/image), checks whether **your** name/roll
number appears, remembers which companies you are eligible for, extracts deadlines,
suppresses duplicate spam, and pushes evidence-backed alerts to **Telegram**.

> Core promise: *"Tell me what matters to me, remember the context, and do not make
> me read the same thing twice."*

## Documentation (`docs/`)

| Doc | Contents |
|---|---|
| `00_Master_Plan.md` | Decision register (DEC-001..009) — the binding contract |
| `01_PRD.md` | Product requirements: user stories, FRs, notification policy, MVP gate |
| `02_TRD.md` | Technical design: architecture, AI layer, matching ladder, security |
| `03_App_Flow.md` | End-to-end runtime flows with error branches |
| `04_UIUX_Brief.md` | Dashboard screens + Telegram alert templates |
| `05_Backend_Schema.md` | Authoritative PostgreSQL schema (source for migrations) |
| `06_Implementation_Plan.md` | P0–P11 build plan (~15 weeks solo) — **start here to build** |

## Repository layout (DEC-007)

```
apps/api          FastAPI: webhooks + dashboard API (uvicorn pia_api.main:app)
apps/worker       Redis job consumers (python -m pia_worker.main)
apps/dashboard    React + TS dashboard (P11) — built by the api Dockerfile, served at "/"
packages/shared   Domain enums, state machines, shared utilities
infrastructure    docker-compose, env template, Alembic migrations
tests             unit / fixtures / integration
docs              The documentation set above
```

## Quickstart (P0)

Prereqs: Docker Desktop, Python 3.11+, (Node 20+ from P11).

```bash
# 1. Environment (never commit .env — SEC-001)
cp infrastructure/env.example infrastructure/.env

# 2. Install backend deps (single editable install)
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"       # Windows
# .venv/bin/pip install -e ".[dev]"         # macOS/Linux

# 3. Boot the stack (postgres + redis + minio + api + worker)
docker compose -f infrastructure/docker-compose.yml up -d

# 4. Schema + fixtures (password = POSTGRES_PASSWORD from infrastructure/.env)
DATABASE_URL="postgresql+psycopg://pia:change-me-postgres@localhost:5432/pia" \
  .venv/Scripts/alembic -c infrastructure/alembic/alembic.ini upgrade head
DATABASE_URL="postgresql+psycopg://pia:change-me-postgres@localhost:5432/pia" \
  .venv/Scripts/python -m tests.fixtures.seed

# 5. Verify
curl http://localhost:8000/health           # {"status": "ok", ...}
pytest                                      # unit suite
PIA_RUN_INTEGRATION=1 pytest -m integration # needs the stack running
```

## Safety invariants (enforced by tests — do not weaken)

- **SEC-005:** `ACTION_AUTOMATION_ENABLED=true` is refused at startup in MVP.
- **ADR-005 / NFR-006:** the eligibility state machine has *no* `AMBIGUOUS → ELIGIBLE`
  edge — ambiguous matches require explicit user confirmation.
- **SEC-003:** webhook routes verify signatures (arrives P1) and are rate-limited.
- **SEC-001:** secrets live in `.env` only; gitleaks runs in CI and pre-commit.
- WhatsApp interaction is **read-only** in MVP (DEC-003). No sends anywhere.

## Git & CI

```bash
git init && git add -A && git commit -m "P0: repository foundation (F-001..F-003)"
pre-commit install        # ruff + gitleaks before every commit
```
CI (`.github/workflows/ci.yml`): ruff → mypy → pytest → gitleaks → docker build.

## Dashboard (P11)

```bash
docker compose -f infrastructure/docker-compose.yml up -d --build api
```

Then open **http://localhost:8000/** and sign in with `DASHBOARD_TOKEN` (the
multi-stage api Dockerfile builds `apps/dashboard` and serves the bundle at "/").
Local UI development: `cd apps/dashboard && npm install && npm run dev`
(proxies /api to localhost:8000).
