# 03 — App Flow

| | |
|---|---|
| Project | **Placement Intelligence Agent (PIA)** |
| Document | End-to-end runtime flows |
| Version | 1.0-draft |
| Date | 2026-09-05 |
| Depends on | `01_PRD.md` (hard), `02_TRD.md` (component names), `00_Master_Plan.md` |
| Consumed by | `04_UIUX_Brief.md`, `06_Implementation_Plan.md` |

**Conventions:** every flow lists error/retry branches (not just happy paths). State names come from the master state machines (§10.1–10.4). Component names come from 02_TRD §1.1.

---

## F0 — Onboarding (first-run setup)

```mermaid
flowchart TD
    A[Open dashboard] --> B[Set admin token + profile basics]
    B --> C[Register Evolution API instance]
    C --> D{Instance connects?}
    D -- no --> D1[Show connection error + retry guidance]
    D1 --> C
    D -- yes --> E[Show QR pairing - recommended SECONDARY number]
    E --> F{Scan successful?}
    F -- no --> E
    F -- yes --> G[Persist connection state CONNECTED - FR-WA-001]
    G --> H[Discover groups - F-006]
    H --> I[Nitish selects groups -> allowlist - FR-WA-004]
    I --> J[Categorize groups - FR-WA-005]
    J --> K[Create profile: name + variants + roll/reg no - FR-PRO-001/002]
    K --> L[Connect Telegram bot token - DEC-002]
    L --> M[Send test notification]
    M --> N{Test received?}
    N -- no --> N1[Surface delivery error, recheck token/chat id]
    N1 --> L
    N -- yes --> O[Onboarding complete -> Overview]
```

| Step | Actor | Component | Action | Output / state |
|---|---|---|---|---|
| 1 | Nitish | Dashboard | First-run wizard | Settings seeded |
| 2 | Nitish | API | Register Evolution instance | `whatsapp_instances` row; connection state persisted (FR-WA-001) |
| 3 | Nitish | Evolution adapter | QR pairing (secondary number — DEC-003) | `CONNECTED`; reconnect failures surfaced |
| 4 | System | API | Group discovery + metadata persist (FR-WA-003) | Group registry rows |
| 5 | Nitish | Dashboard | Allowlist selection (FR-WA-004) + category (FR-WA-005) | Only selected groups ever reach AI pipeline (SEC-006) |
| 6 | Nitish | Dashboard | Profile: canonical name, aliases/misspellings, roll/reg/student ID (FR-PRO-001/002) | `candidate_profiles` + `identity_aliases` |
| 7 | Nitish | Dashboard | Telegram bot token + chat ID; test message (DEC-002) | Delivery confirmed before first real alert |

**Error branches:** instance unreachable → retryable status shown; QR timeout → re-pair loop; Telegram test failure → delivery error surfaced, onboarding blocked (alerts must not silently fail later).

---

## F1 — Message Ingestion (every WhatsApp event)

```mermaid
sequenceDiagram
    participant EV as Evolution API
    participant API as Ingestion Gateway
    participant DB as PostgreSQL
    participant Q as Redis queue
    participant W as Worker

    EV->>API: POST /webhooks/evolution (+signature)
    API->>API: Verify signature + rate limit (SEC-003)
    alt invalid signature / rate limited
        API-->>EV: 401 / 429 (event dropped, logged)
    end
    API->>API: Normalize -> NormalizedMessage (FR-MSG-001)
    alt group not in allowlist
        API->>DB: raw audit row only (SEC-006, retention)
        API-->>EV: 202 (no pipeline processing)
    end
    API->>DB: idempotency check provider_id + content_hash (FR-MSG-002)
    alt duplicate delivery
        API-->>EV: 202 (no new record/job)
    end
    API->>DB: INSERT message state=RECEIVED + raw payload (FR-MSG-003)
    API-->>EV: 202 Accepted (durable before ACK)
    API->>Q: enqueue classify + (if media) process_attachment
    DB->>DB: state=QUEUED (FR-MSG-004)
    W->>Q: consume
    DB->>DB: state=PROCESSING
    alt job fails (transient)
        W->>Q: requeue with backoff, state=RETRYING
        alt retries exhausted
            W->>DB: state=FAILED + reason -> DLQ
        end
    else success
        W->>DB: state=PROCESSED
    end
```

Key rules: response `202` only **after** durable persistence (NFR-001); 10 identical deliveries → exactly one logical message (FR-MSG-002 acceptance); every state transition queryable (FR-MSG-004); terminal `FAILED` always carries a visible reason (master §10.1).

---

## F2 — Media / Document Processing

```mermaid
flowchart TD
    A[Attachment metadata on message] --> B{Download permitted?}
    B -- no --> Z[Record reason, no fetch - FR-WA-006]
    B -- yes --> C[Download from Evolution]
    C -- fail --> C1[Retryable job, backoff]
    C1 -- retries exhausted --> C2[FAILED + DLQ - never silent drop]
    C -- ok --> D[Store to MinIO + checksum]
    D --> E{Size <= MEDIA_MAX_SIZE_MB?}
    E -- no --> E1[REJECTED_OVERSIZE visible in Documents]
    E -- yes --> F{File type}
    F -- XLSX/XLS --> G1[pandas/openpyxl parse - FR-ELG-002]
    F -- CSV --> G2[pandas parse - FR-ELG-002]
    F -- PDF text --> G3[PyMuPDF - FR-ELG-003]
    F -- scanned PDF --> G4[OCR then vision fallback]
    F -- image --> G5[OCR then vision fallback - FR-ELG-004]
    G1 & G2 & G3 & G4 & G5 --> H[Header detection + CandidateRow normalization - TRD §5]
    H --> I{Extraction confidence ok?}
    I -- low --> J[Vision fallback]
    J -- still low --> K[NEEDS_REVIEW state - shown on Documents screen]
    I -- yes --> L[Persist document_extractions + evidence]
    L --> M[Trigger eligibility analysis - F3]
```

Failure guarantees: failed downloads are retryable jobs, never silent drops (FR-WA-006); low-confidence extractions stop at `NEEDS_REVIEW` and never auto-match (ADR-005).

---

## F3 — Eligibility Detection & Matching (the core flow)

```mermaid
flowchart TD
    A[Message + extraction CandidateRows] --> B[Eligibility detector AI task + rules]
    B --> C{is_candidate_list?}
    C -- no --> Z[No eligibility processing]
    C -- yes --> D[Resolve company from text/filename - FR-MEM-001]
    D --> E[Run matching ladder - TRD §6]
    E --> F{MatchResult.status}
    F -- MATCHED --> G[EligibilityRecord state=ELIGIBLE + confidence + evidence - FR-ELG-008]
    G --> G1[One CRITICAL eligibility alert]
    F -- AMBIGUOUS --> H[Record state=AMBIGUOUS, requires_user_review=true]
    H --> H1[Alert asks Nitish to confirm/deny - NEVER auto-ELIGIBLE ADR-005]
    H1 --> H2{User decision}
    H2 -- confirm --> H3[USER_CONFIRMED -> ELIGIBLE - audited]
    H2 -- deny --> H4[Correction stored - FR-PRO-004, feedback loop]
    F -- NOT_FOUND --> I[Record state=NOT_FOUND - explicitly NOT ineligibility FR-ELG-009]
    F -- INVALID_SOURCE --> J[Extraction marked invalid, review]
```

| Branch | Guarantee |
|---|---|
| MATCHED | Evidence chain: message → document → page/sheet/row (FR-ELG-008); one alert (dedup applies) |
| AMBIGUOUS | Structurally cannot become ELIGIBLE without user action (NFR-006) — same-name fixture test at P6 |
| NOT_FOUND | Distinct from NOT_ELIGIBLE; no negative claim (FR-ELG-009) |
| INVALID_SOURCE | Visible in Documents screen with failure reason |

---

## F4 — Company Memory Linkage

```mermaid
flowchart TD
    A[New message entities: company mentions] --> B{Known company?}
    B -- yes, via alias --> C[Resolve to canonical company - FR-MEM-003]
    B -- no --> D[Create company entity + aliases - FR-MEM-001]
    D --> C
    C --> E{Eligibility state for user?}
    E -- ELIGIBLE/USER_CONFIRMED --> F[Watch state ACTIVE - FR-MEM-005]
    F --> G[All future company messages prioritized automatically]
    E -- UNKNOWN --> H[Normal classification path]
    E -- NOT_ELIGIBLE (explicit source only) --> I[Lower priority, still tracked]
    C --> J[Update application lifecycle if event indicates stage - FR-MEM-004]
```

Acceptance echo: once Accenture is ELIGIBLE, "Accenture OA" resolves to the Accenture record and is auto-prioritized without manual re-subscription (FR-MEM-003/005).

---

## F5 — Event & Deadline Lifecycle

```mermaid
flowchart TD
    A[Message entities: dates/links/actions] --> B[Event extraction FR-EVT-001/002]
    B --> C{Date present in source text?}
    C -- no --> D[Event with unknown time - NEVER invented FR-EVT-005]
    C -- yes --> E[Resolve relative date in Asia/Kolkata - stored with timezone]
    E --> F[Canonical event created: canonical_key = company+type+action+deadline+link]
    F --> G{Semantic near-match to existing event? FR-DED-003}
    G -- no --> H[New canonical event, deadline OPEN - FR-EVT-003]
    G -- yes, facts identical --> I[Duplicate suppressed - link to canonical event]
    G -- yes, facts changed --> J[Delta update FR-EVT-004: new deadline/link/venue/time]
    J --> K[event_updates row + one delta notification]
    H --> L{Deadline approaches}
    L -- T-24h/T-6h/T-1h --> M[DUE_SOON escalation reminders - FR-NOT-005]
    L -- due time passes --> N[EXPIRED]
    L -- user completes / result seen --> O[COMPLETED]
    L -- authoritative cancellation --> P[CANCELLED]
```

State machine (master §10.3): `DETECTED → ACTIVE → COMPLETED | UPDATED → ACTIVE | CANCELLED | EXPIRED`. Hallucination guard: validators reject any date string absent from source text (FR-EVT-005, ADR-004).

---

## F6 — Notification Decisioning

```mermaid
flowchart TD
    A[New/updated canonical event or eligibility] --> B[Relevance scorer - FR-NOT-001]
    B --> C{Priority?}
    C -- CRITICAL --> D[Immediate send - e.g. deadline today, KYC, confirmed-eligible action needed]
    C -- HIGH --> E[Immediate send - OA, shortlist, reg deadline >24h, eligible-company event FR-NOT-002]
    C -- MEDIUM --> F[Daily digest queue]
    C -- LOW --> F
    C -- IGNORE --> X[SUPPRESS - visible in history with reason]
    D & E --> G{dedup_key seen? - FR-NOT-004}
    G -- yes --> X
    G -- no --> H[Render template master §11.1 + evidence links FR-NOT-007]
    H --> I[Telegram adapter send - DEC-002]
    I -- fail --> I1[Retry, then PENDING_DELIVERY shown on Overview]
    I -- ok --> J[Record delivery + status]
    F --> K[Digest job 20:30 IST]
```

Escalation branch: a deadline moving to `DUE_SOON` re-enters this flow; dedup keys include the escalation window, so reminders are intentional, not spam (FR-NOT-005). Critical academic notices take the same CRITICAL path even with no company relation (FR-NOT-003).

---

## F7 — Daily Digest

1. Digest job runs at configured local hour (default 20:30 Asia/Kolkata).
2. Query canonical events updated since last digest with priority MEDIUM/LOW **not already delivered** (FR-NOT-006).
3. Summarizer AI task composes sections from **stored facts only** (validator rejects added facts — TRD §4.2 #6).
4. Sections: 🔥 urgent · 🏢 placement · 🎓 academic · 📌 tracked companies.
5. Render → Telegram → record delivery. Empty digest → suppressed (no "nothing today" noise) unless setting enabled.

---

## F8 — User Correction / Feedback

```mermaid
flowchart TD
    A[Nitish reviews a match on Documents/Companies screen] --> B{Correction type}
    B -- false match --> C[POST /feedback/match wrong_match]
    B -- false negative --> D[POST /feedback/match missed_match + evidence]
    C --> E[EligibilityRecord ELIGIBLE -> SUPERSEDED w/ reason - master §10.2]
    D --> F[Create/confirm ELIGIBLE with user evidence]
    E & F --> G[Audit log entry actor=user, source=correction - FR-PRO-004]
    G --> H[Feedback stored for matching evaluation - threshold tuning data ADR-009]
    H --> I[Downstream: watch state off/on, future alerts adjust immediately]
```

Corrections are reversible state transitions with full evidence; they also feed the fixture corpus for threshold evaluation (Q8).

---

## F9 — Dashboard Navigation (screen ↔ flow map)

| Screen (master §17) | Primary flows surfaced |
|---|---|
| Overview | F6 urgent items, today's deadlines (F5), recent updates, tracked companies |
| Companies | F4 lifecycle + eligibility state, latest event, next deadline |
| Timeline | F5 canonical event history (FR-DED-005) |
| Inbox | F1/F3 AI-filtered important messages with source evidence |
| Documents | F2 extractions, match results, NEEDS_REVIEW queue |
| Notifications | F6 delivered + suppressed with reasons, delivery status |
| Profile | F0 identity/aliases/documents |
| Settings | F0 groups, thresholds, notification windows, retention, provider config |
| Audit | F8 corrections, state changes, (future) action approvals |

---

## F10 — Flagship Golden End-to-End Scenario (master §20.2)

```mermaid
sequenceDiagram
    participant G as WhatsApp group
    participant P as PIA pipeline
    participant U as Nitish (Telegram)

    G->>P: "Accenture eligible list attached" (XLSX/image/PDF)
    P->>P: Detect candidate list (FR-ELG-001) + parse rows (FR-ELG-002..004)
    P->>P: Matching ladder finds user (exact/normalized/ID evidence)
    P->>P: EligibilityRecord = ELIGIBLE + evidence (FR-ELG-008)
    P->>U: 1 eligibility alert (CRITICAL) with source reference
    Note over P: 2 hours later
    G->>P: "Accenture OA tomorrow 10 AM"
    P->>P: Company resolver links to Accenture (FR-MEM-003)
    P->>P: Event engine creates OA event, deadline tz-normalized (FR-EVT-002)
    P->>P: Relevance: existing ELIGIBLE state -> HIGH (FR-NOT-002)
    P->>U: 1 OA alert
    Note over P: group repeats same message 3x
    P->>P: Dedup suppresses repeats (FR-DED-001..003) - no new alerts
    Note over P: later: "OA moved to 11 AM"
    P->>P: Delta detected (FR-EVT-004) -> event updated
    P->>U: 1 update alert (time changed 10→11 AM)
```

This single scenario exercises F1→F2→F3→F4→F5→F6 including dedup and delta — it is the P10/P11 capstone E2E test (06_Implementation_Plan §3).

---

## Traceability

| Flow | Requirement groups covered |
|---|---|
| F0 | FR-WA-001..005, FR-PRO-001/002, DEC-002/003 |
| F1 | FR-WA-002/006, FR-MSG-001..004, SEC-003/006, NFR-001/002 |
| F2 | FR-WA-006, FR-ELG-002..004, ADR-005 |
| F3 | FR-ELG-001..009, FR-MEM-001, FR-PRO-004, ADR-005 |
| F4 | FR-MEM-001..005 |
| F5 | FR-EVT-001..005, FR-DED-003/004, FR-NOT-005 |
| F6 | FR-NOT-001..004, FR-CLS-002, DEC-002, §11 policy |
| F7 | FR-NOT-006 |
| F8 | FR-PRO-004, FR-MEM-002, ADR-009 |
| F9 | Master §17 screens |
| F10 | Master §20.2 golden scenario |

## Open Questions
- Exact Evolution webhook payload fields affect F1 normalization mapping only — resolved at P1 (Q4).
