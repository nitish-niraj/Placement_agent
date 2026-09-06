# 06 — Implementation Plan

| | |
|---|---|
| Project | **Placement Intelligence Agent (PIA)** |
| Document | Solo-developer build plan (MVP P0–P11) |
| Version | 1.0-draft |
| Date | 2026-09-05 |
| Depends on | `01_PRD.md` + `02_TRD.md` (hard), `05_Backend_Schema.md` (soft — schema work is itself F-002), `00_Master_Plan.md` DEC-004/007/009 |
| Consumed by | You, starting today. Execute top to bottom. |

**Operating rule (master §19):** do not start the next feature until the current one meets its acceptance criteria and has unit + integration tests. Each milestone ends with a working demo, logs/metrics, migration if needed, and documentation. Phase order is strict per ADR-010 (deviations require a recorded ADR amendment — see §7).

---

## 1. Milestone Map (P0–P11, ~14–15 weeks for one solo developer)

| Milestone | Name | Features | Duration | Leaner-doc phase (DEC-004 mapping) | Demo checkpoint |
|---|---|---|---|---|---|
| P0 | Repository & infrastructure foundation | F-001, F-002, F-003 | 1 wk | — | `docker compose up` boots api+worker+postgres+redis+minio; `/health` green; CI green |
| P1 | Evolution API connector | F-004, F-005, F-006 | 1 wk | Phase 0 (part) | Live group message reaches the internal webhook; connection state visible |
| P2 | Message persistence & idempotency | F-007, F-008 | 1 wk | Phase 0 (rest) | 10 duplicate webhook deliveries → 1 stored logical message |
| P3 | Basic intelligence (classify + extract) | F-009 | 1.5 wk | Phase 1 (part) | Regression dataset classified at ≥ defined threshold; entities extracted with null-when-unknown |
| P4 | Candidate profile | F-010 | 0.5–1 wk | Phase 0 prep (§6 data) | Profile CRUD + audit trail; aliases usable by matcher tests |
| P5 | Document intelligence | F-011, F-012, F-013, F-014 | 2 wk | Phase 2 (part) | Fixture suite: XLSX/CSV/PDF/scanned-PDF/image all → CandidateRows with evidence |
| P6 | Eligibility engine | F-015, F-016 | 1.5 wk | Phase 2 (rest) | Known fixtures match correctly; ambiguous same-name fixture stops at review |
| P7 | Company memory | F-017, F-018 | 1 wk | Phase 3 (part) | "Accenture OA" after Accenture eligibility auto-links and prioritizes |
| P8 | Event/deadline engine | F-019, F-020, F-021 | 1.5 wk | Phase 3 (rest) | Deadline changes update canonical event; no date in text → no date stored |
| P9 | Semantic dedup | F-022 | 1 wk | Phase 4 | Repeated reminders suppressed; 7-day-old repeat re-notifies; delta still alerts |
| P10 | Notification engine | F-023, F-024, F-025 | 1.5 wk | Phase 1 (digest slice) | Golden E2E scenario (03_App_Flow F10) passes end-to-end to Telegram |
| P11 | Dashboard | F-026, F-027 + screens per 04_UIUX | 2 wk | — | Every important decision inspectable with evidence on the 9 screens |
| — | **MVP total** | | **≈ 15 wk** (10 wk aggressive / 16 wk with buffer) | | MVP release gate (§6) |

Post-MVP (deferred, not scheduled): P12 conversational search (F-028), P13 action preparation (F-029, F-030), P14 controlled automation (F-031) — see §2.4.

---

## 2. Per-Milestone Task Breakdown

Format: task → acceptance criteria → traced requirements. Every task also obeys the §4 DoD checklist.

### 2.1 P0 — Repository & infrastructure foundation (1 wk)

**F-001 Repository bootstrap**
- [ ] Monorepo per DEC-007: `apps/api`, `apps/worker`, `apps/dashboard`, `packages/shared`, `infrastructure`, `tests`, `docs`
- [ ] Python 3.11 + uv/pip-tools; ruff + mypy configured (strict on `packages/shared`); pre-commit with gitleaks (SEC-001)
- [ ] `infrastructure/docker-compose.yml` with api, worker, postgres:15, redis:7, minio, caddy (TRD §11)
- [ ] `packages/shared`: domain enums + state machines (05_Backend_Schema §2) as the single source
- **Accept:** fresh clone → `docker compose up` → all services healthy (P0 exit criterion, master §18)

**F-002 PostgreSQL baseline**
- [ ] Alembic wired; initial migration implements 05_Backend_Schema §3 (all tables + enums + indexes)
- [ ] UUID PKs, tz-aware timestamps, audit fields, partial indexes
- **Accept:** `alembic upgrade head` + `downgrade -1` both clean; seed command loads fixtures

**F-003 Redis/job baseline**
- [ ] Queue abstraction (enqueue/consume/retry/backoff/DLQ) + `job_records` + `dead_letter_queue` tables
- [ ] Structured logging with correlation IDs (TRD §10.1); `/health` composition (TRD §10.4)
- **Accept:** injected test job retries 3× then lands in DLQ with error class + correlation ID

### 2.2 P1 — Evolution API connector (1 wk)

**F-004 Evolution instance registration**
- [ ] POST/GET `/instances`; connection state persisted through `instance_status` transitions (FR-WA-001)
- [ ] QR pairing flow in dashboard; **secondary-number recommendation displayed** (DEC-003)
- **Accept:** instance paired via QR; `last_seen_at` updates; disconnect surfaced

**F-005 Webhook receiver**
- [ ] `POST /webhooks/evolution`: HMAC/shared-secret verification + rate limit (SEC-003); normalized envelope
- [ ] 202-after-durable-persist pattern (NFR-001); invalid signature → 401 + audit row
- **Accept:** live group message hits the internal webhook and is normalized (P1 exit criterion)

**F-006 Group discovery**
- [ ] Pull group list from Evolution; store metadata (FR-WA-003); allowlist toggles + category (FR-WA-004/005)
- **Accept:** message from non-enabled group never enters the pipeline (test asserts zero AI calls — SEC-006)

### 2.3 P2 — Message persistence & idempotency (1 wk)

**F-007 Message persistence** — canonical `messages` + `attachments` tables (FR-MSG-001); unit tests for missing/optional provider fields; media download → MinIO with checksum, failure → retryable job (FR-WA-006). **Accept:** every message + attachment metadata queryable with correlation ID.

**F-008 Idempotent processing** — `UNIQUE(provider_message_id, group_id)` + `ON CONFLICT` upsert + `idempotency_keys` (TRD §4); processing states wired end-to-end (FR-MSG-004); raw payload retention rows (FR-MSG-003). **Accept:** 10 identical deliveries → 1 logical message + 1 job set (NFR-002, P2 exit criterion).

### 2.4 P3 — Basic intelligence (1.5 wk)

**F-009 Classifier v1** (FR-CLS-001..004)
- [ ] Rule-based first pass (keywords/patterns per group) → LLM classification with `Classification` schema (TRD §4.2 #1); override precedence rules > rules > LLM, auditable
- [ ] Entity extractor with `Entities` schema; null-when-unknown enforced by validator (FR-CLS-003, FR-EVT-005)
- [ ] Build regression dataset (~100 labeled messages from real groups/fixtures) + `ai_call_logs` instrumentation
- **Accept:** dataset classified with defined quality threshold (P3 exit criterion); low-confidence ≠ high importance (FR-CLS-001)

### 2.5 P4 — Candidate profile (0.5–1 wk)

**F-010 Candidate profile** (FR-PRO-001..003)
- [ ] CRUD + encrypted sensitive fields (SEC-002); aliases/identifiers management; audit trail on every change
- [ ] Name normalization utilities in `packages/shared` (FR-ELG-005 foundations)
- **Accept:** profile changes audited; normalization unit tests pass (P4 exit: profile usable by eligibility engine)

### 2.6 P5 — Document intelligence (2 wk)

**F-011 XLSX parser** — openpyxl/pandas; header variant detection; multi-sheet; → `CandidateRow[]` + sheet evidence (FR-ELG-002). **Accept:** fixture XLSX files parse with correct rows/columns.
**F-012 PDF parser** — PyMuPDF text/page extraction; scanned-PDF → OCR path (FR-ELG-003). **Accept:** match evidence includes page reference.
**F-013 Image OCR** — OCR with confidence; low confidence → vision fallback (FR-ELG-004). **Accept:** image fixtures produce rows with region evidence or NEEDS_REVIEW.
**F-014 Vision fallback** — vision model per TRD §4.2 #4 (`VisionExtraction` schema); ambiguous names stay ambiguous. **Accept:** `NEEDS_REVIEW` state surfaced, never auto-matched (ADR-005).
- Fixture suite (P5 exit criterion): ≥ 2 XLSX, 1 CSV, 2 PDF (text + scanned), 2 screenshots — real-world-like, with expected candidate rows.

### 2.7 P6 — Eligibility engine (1.5 wk)

**F-015 Identity matcher** — ladder EXACT → NORMALIZED → IDENTIFIER → FUZZY → MODEL_REVIEW (TRD §6); `MatchResult` contract; thresholds tuned on fixture corpus + rationale documented (ADR-009, Q8). **Accept:** known fixtures match via expected method; ambiguous cases stop.
**F-016 Eligibility record** — persist MATCHED/AMBIGUOUS/NOT_FOUND/INVALID_SOURCE; `eligibility_records` state machine (§10.2); correction endpoint `POST /feedback/match` (FR-PRO-004). **Accept:** NOT_FOUND ≠ NOT_ELIGIBLE (FR-ELG-009); AMBIGUOUS→ELIGIBLE impossible without user action (NFR-006).

### 2.8 P7 — Company memory (1 wk)

**F-017 Company canonicalization** (FR-MEM-001/003) — company entities + aliases; mention resolution (mention/aliases/group context). **Accept:** Accenture/Accenture India variants resolve to one entity.
**F-018 Company watch** (FR-MEM-002/004/005) — eligibility state persistence with evidence; watch state auto-activation; lifecycle stage tracking. **Accept:** eligible-company messages auto-prioritized without re-subscription (P7 exit criterion).

### 2.9 P8 — Event/deadline engine (1.5 wk)

**F-019 Event extraction** (FR-EVT-001) — 12 event types; canonical events with `canonical_key`. **Accept:** structured event record per type.
**F-020 Deadline extraction** (FR-EVT-002/003/005) — absolute + relative dates resolved in Asia/Kolkata; deadline objects with states; **missing date stays null** (validator test). **Accept:** ≥1 deadline normalized to Asia/Kolkata (release gate item).
**F-021 Event update/delta** (FR-EVT-004) — delta detection → `event_updates` + canonical event update. **Accept:** changed deadline/link/venue updates the event (P8 exit criterion).

### 2.10 P9 — Semantic dedup (1 wk)

**F-022 Duplicate engine** (FR-DED-001..005, DEC-009)
- [ ] Exact (content hash) → near-duplicate (text similarity) → semantic (fact tuple) → pHash for images
- [ ] 7-day window rule; canonical event history with suppression reasons
- **Accept:** repeated reminders suppressed; material delta still alerts (P9 exit criterion); `duplicate_suppressed_total` metric live

### 2.11 P10 — Notification engine (1.5 wk)

**F-023 Notification decision** (FR-NOT-001..005) — relevance scoring; §11 priority mapping; dedup keys; escalation windows. **Accept:** eligible-company OA produces HIGH/CRITICAL alert (FR-NOT-002).
**F-024 Notification delivery** (FR-NOT-007, DEC-002) — Telegram adapter + delivery history + evidence links + pending-delivery backup path. **Accept:** alert arrives with template per §11.1.
**F-025 Digest** (FR-NOT-006) — daily job, canonical-events-only composition, empty-digest suppression. **Accept:** digest contains only new/actionable items.
- **Capstone:** golden E2E scenario (03_App_Flow F10 = master §20.2) passes as automated test — this is the P10 exit criterion.

### 2.12 P11 — Dashboard (2 wk)

**F-026 Dashboard overview** (master §17) — all 9 screens per 04_UIUX_Brief; Overview personal cockpit first, then Companies/Timeline/Inbox/Documents/Notifications, then Profile/Settings/Audit. **Accept:** user can inspect every important decision (P11 exit criterion).
**F-027 Company timeline** — evidence-backed per-company history merging events, updates, notifications. **Accept:** timeline shows original + updates (FR-DED-005).
- Dashboard auth: single bearer token (Q3 default); states per 04_UIUX §7 incl. stale-connection dot.

### 2.13 Deferred (post-MVP — do not schedule)

| Feature | Phase | Note |
|---|---|---|
| F-028 Conversational search | P12 | Needs P0–P11 stable; source-backed answers only |
| F-029 KYC/form detector | P13 | Detection/reminders only — **KYC automation permanently excluded (DEC-008)** |
| F-030 Approval workflow | P13 | PROPOSED→…→SUCCEEDED machine already modeled in schema |
| F-031 Browser/action executor | P14 | `ACTION_AUTOMATION_ENABLED=false` until a future ADR says otherwise |

---

## 3. Test Strategy per Milestone (master §20)

| Layer | Applied at | Content |
|---|---|---|
| Unit | every P | normalization, date parsing, identity normalization, match scoring, dedup keys, state transitions |
| Fixture | P5, P6, P9 | real-world-like XLSX/PDF/image eligibility lists with expected matches; pHash fixtures |
| Integration | P1–P3, P10 | Evolution webhook → DB → queue → processing (testcontainers) |
| End-to-end | P10, P11 | message → eligibility → company memory → deadline → notification (golden scenario, §20.2) |
| AI regression | P3+ | fixed prompt/model test set with structured-output validation; runs on model/provider change |
| Safety | P6, P9 | same-name ambiguity, missing evidence, stale list, contradictory updates, duplicate reminders, failed OCR — **ambiguous fixture never auto-confirms (100%, NFR-006)** |
| Chaos | P2+, grow in P9/P10 | duplicate webhook burst, worker restart mid-job, queue backlog, media timeout, DB restart |

CI runs unit + fixture + integration on every PR; E2E + chaos nightly and pre-tag.

## 4. Definition of Done — per feature gate (master §21)

- [ ] Requirement ID mapped to implementation task(s)
- [ ] Happy path implemented · [ ] Failure paths implemented
- [ ] Unit tests added · [ ] Integration tests when crossing boundaries
- [ ] No secrets/logging leaks · [ ] Migration added, reversible where practical
- [ ] Observability: log + metric + error state · [ ] User-visible behavior documented
- [ ] Acceptance criteria demonstrated with a reproducible test · [ ] Regression suite passes
- [ ] No unrelated feature bundled

## 5. CI/CD & Workflow

**Pipeline (GitHub Actions or equivalent):**
1. lint (ruff) → 2. type check (mypy) → 3. tests (pytest; services via testcontainers) → 4. secret scan (gitleaks — SEC-001/NFR-008) → 5. docker build (api/worker/dashboard images) → 6. deploy (manual `docker compose pull && up -d` on the VPS for MVP).

**Solo-dev conventions:** trunk-based with short-lived feature branches (`feat/f-015-identity-matcher`); PRs opened even for self-review (the DoD checklist lives in the PR template); main always deployable; one feature = one PR (master DoD). Pre-commit hooks mirror CI to fail fast.

## 6. Local Dev Setup (first-day runbook)

1. Install prerequisites: Docker Desktop, Python 3.11+, Node 20+.
2. `git clone` → `cp infrastructure/env.example infrastructure/.env` → fill secrets (never commit — SEC-001).
3. `docker compose up` → `alembic upgrade head` → `python -m tests.fixtures.seed`.
4. Create Evolution instance; pair the **primary WhatsApp number** (owner decision 2026-09-05; a secondary number remains the safer recommendation — see DEC-003 update) via QR.
5. Configure `NVIDIA_API_KEY` (NVIDIA NIM, P3) + `TELEGRAM_BOT_TOKEN`/chat id; send a test notification.
6. `pytest` green → start building the current milestone.

**Deployment (Q2 resolved 2026-09-05):** the same compose file runs on a small always-on VPS (~2 GB RAM) — copy `infrastructure/env.example` → `.env` with production secrets, serve behind Caddy with TLS. The VPS must stay online continuously: Evolution API only catches group messages while connected.

## 7. Risk-Based Reordering Notes (ADR-010 discipline)

The leaner doc's value-first order (digest early, dedup polish interleaved) conflicts in places with strict P0→P11 ordering (ADR-010). Resolutions:

- **Early digest value (leaner Phase 1):** the digest *slice* (F-025) lands in P10 as planned; if early value is critical, file **ADR-011 amendment**: *"F-025 basic digest (rule-based, no semantic dedup) may be delivered after P3, superseded by F-022/F-023 in P9/P10."* Proposed text recorded here — adopt only consciously.
- **Dedup polish interleaved (leaner Phase 4):** exact-hash dedup is already part of F-008 (P2); semantic dedup stays in P9 (F-022). No reorder needed.
- **Connector risk early:** Evolution API pin (Q4) validated in P1 week 1 precisely because connector failure is the highest external risk (master §23).

## 8. MVP Release Gate → Satisfying Milestone (master §26)

| Gate item | Satisfied by |
|---|---|
| ≥1 real/fixture group ingested end-to-end | P2 (F-007/F-008) |
| ≥1 XLSX, 1 PDF, 1 image list processed | P5 (F-011..F-014) |
| Correct match on known fixtures | P6 (F-015/F-016) |
| Ambiguous same-name never auto-confirms | P6 safety tests (NFR-006) |
| Company memory links later updates | P7 (F-017/F-018) |
| ≥1 deadline normalized to Asia/Kolkata | P8 (F-020) |
| Repeat → 1 notification; delta → 1 update | P9 + P10 |
| Academic announcement surfaced regardless of placement classification | P3 + P10 (FR-CLS-002, FR-NOT-003) |
| Every critical/high alert has evidence | P10 (FR-NOT-007) |
| Failure/retry paths observable | P0/P2 (DLQ, states, `/health`) |
| No secrets in logs/source | P0 CI (gitleaks) onward |
| Automated actions disabled | P0 config default; re-verified at release |

## 9. Traceability

All F-001..F-031 assigned above (F-028..F-031 deferred); every task carries its FR IDs; milestones = master §18 P0–P11; demo checkpoints per milestone; gate mapping in §8. Timeline assumption: ~20 focused hrs/wk; at 40 hrs/wk halve the durations (≈ 7–8 weeks).
