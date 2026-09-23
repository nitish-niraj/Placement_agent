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

## Ask PIA (P12)

Post a natural-language question — answers come strictly from stored data with
message citations (pgvector semantic retrieval + deterministic keyword search;
the LLM only phrases the answer and its failure path returns raw evidence):

```bash
curl -X POST http://localhost:8000/api/v1/ask   -H "Authorization: Bearer $DASHBOARD_TOKEN"   -H "Content-Type: application/json"   -d '{"question": "what is the TECHADEMY package and role?"}'
```

The dashboard Ask screen also has a 🧠 **Deep (multi-step agent)** toggle —
ADR-011 Stage 1: a bounded ReAct loop (max 6 steps) over read-only tools
(search messages, eligibility, events, deadlines, company timeline, profile);
answers restate tool results with citations, the reasoning trace is shown and
stored in `agent_traces`, and any LLM failure degrades to the single-shot
ladder below.

**Stage 2 — the daily reviewer:** 30 minutes after the nightly digest, a
bounded agent pass reviews your deadlines, eligibility, and events and files
up to 3 evidence-backed proposals on the Approvals screen (types: deadline
nudge, verify-field, data-quality, KYC reminder, follow-up), with a Telegram
summary. It proposes, never acts on its own — approving a proposal hands it to a
deterministic Stage 3 executor (Telegram reminder card, approved event-field
correction, or a deterministic re-parse — ADR-013,
`STAGE3_EXECUTORS_ENABLED=false` turns them off). Every run is audited.

**Stage 4 (built, dormant):** messages the classifier can't confidently place
can get one agent second opinion (escalate / digest / ignore — rule veto,
confidence threshold, daily cap, audited). Off by default
(`STAGE4_REACTIVE_ENABLED=true` to enable).

Also available as the "Ask PIA" screen in the dashboard. The answer rides the
**DEC-010 ladder** (NIM structured → OpenRouter → Groq → deterministic stored
evidence); each result carries a `source` field naming the rung that answered
(`nim` / `openrouter` / `groq` / `deterministic`). Simple ordering questions
("which was the latest company I was eligible for?") are answered from stored
records by `detected_at` even when every LLM rung is down. Embeddings refresh
hourly with the maintenance sweep; ADR-003 applies — answers are informational
and never mutate eligibility/events state.

## Ask PIA on Telegram

The `telegram-ask` compose service long-polls the bot (no inbound port): any
text message from **`TELEGRAM_CHAT_ID` only** (fail-closed for every other
chat) is answered by the same `ask_question` ladder, with confidence, rung
tag, and up to 3 cited sources. `/start` shows usage. Offset is acked through
Redis so restarts don't re-answer. Disable with `TELEGRAM_ASK_ENABLED=false`.

```bash
docker compose -f infrastructure/docker-compose.yml up -d --build telegram-ask
docker logs pia-telegram-ask-1 -f        # watch it answer
```

## Approvals (P13)

Placement form requests become FORM events with reminders, and each one
proposes a `form_draft` action pre-filled from your stored profile — shown on
the dashboard **Approvals** screen. The Teams listener feeds the same
pipeline: a form link dropped in the meeting chat (with the caption-detected
teacher/presenter name) becomes a draft too.

With `FORM_AUTOMATION_ENABLED=true`, each draft has a **Fill & preview**
button: the portal opens the real Google Form inline, pre-filled from your
stored profile (identity facts filled, judgement questions left blank for
you), and **you click Submit yourself** — the server never fills or submits
anything. The company's real KYC
session stays always-manual (DEC-008). Nothing is ever submitted without your
explicit per-draft approval (ADR-008); approving only records the decision
(audited). KYC
*session attendance* remains always-manual (DEC-008).

## Teams listener ops (2026-09-22 hygiene)

```powershell
# Join a session (sources infrastructure/.env for TEAMS_* + LLM keys)
.\scripts\run-listener.ps1 -MeetingUrl "<teams link>" -Minutes 4

# Re-save the session when the join falls back to guest/"Unverified"
.\scripts\refresh-session.ps1 -MeetingUrl "<teams link>"   # manual, MFA-safe
.\scripts\refresh-session.ps1 -Auto -MeetingUrl "<teams link>"  # programmatic
# (replaces archived E:\agent\autologin.py; --auto reuses the listener's
# sign-in driver, verifies the LPU address, never saves on failure)

# Transcript retention (90+ files accumulate fast; dry-run by default)
.\scripts\clean-transcripts.ps1 -Keep 30          # preview
.\scripts\clean-transcripts.ps1 -Apply -Keep 30   # delete older
```

Throwaway probes/logs from Sept 12 (`probe*.py`, `listen_run*.log`) live in
`.attic-2026-09-12/` (gitignored). Shared URL helpers: `teams/_shared.py`
(`listener.py` re-exports them, so existing imports keep working).

## Application state (DEC-011)

Company mentioned ≠ you applied. When eligibility is established, PIA asks
("Have you applied for this role?" — Telegram buttons or dashboard
`POST /api/v1/applications/answer`) and stores the answer per company+role
(`APPLIED / NOT_APPLIED / NOT_SURE / NOT_INTERESTED`). Afterwards:

- post-application updates (confirmations, verifications, penalties) become
  follow-ups **only if you applied** for that exact role;
- anything else (openings, other roles, lists without your name) stays quiet
  — attached CSV/image lists lacking your name strictly suppress FORM/KYC/
  registration notifications, drafts, and reminders.

## Notification pipeline (DEC-012)

Every alert is built from a normalized context (subject, topic, attachment
verification, deadline truth, recommendation, evidence) and rendered for a
student phone screen — never raw classification fields:

- subjects resolve company → attachment filename → topic token (e.g. MARS)
  → `📋 Placement Update`; "General" is gone;
- defaulter lists are verified row-by-row: in-list → `🔴 ACTION REQUIRED`,
  absent → silence, unverifiable → `⚠️ VERIFICATION NEEDED` (never a guess);
- deadlines render truthfully (`⚫ EXPIRED — passed on …`, never stale
  "TODAY"); unknown stays "Not specified";
- the digest is categorized (action/deadlines/interviews/tests/
  registrations/info) and sent at most once per day;
- watcher/dry-run chatter and worker tracebacks never reach your chat —
  internals go to logs and the optional `ADMIN_TELEGRAM_CHAT_ID`.
