# 01 — Product Requirements Document (PRD)

| | |
|---|---|
| Project | **Placement Intelligence Agent (PIA)** |
| Document | Product Requirements Document |
| Version | 1.0-draft |
| Date | 2026-09-05 |
| Owner / User | Nitish (single user) |
| Sources | `docs/_source/requirements_raw.txt` — *Placement Intelligence Agent Master Requirements* v1.0 (2026-08-24, authoritative) + *Placement WhatsApp Assistant Requirements* (pragmatic/safety guidance) |
| Depends on | `docs/00_Master_Plan.md` (Decision Register DEC-001..009 is binding) |
| Related docs | 02_TRD · 03_App_Flow · 04_UIUX_Brief · 05_Backend_Schema · 06_Implementation_Plan |

---

## 1. Executive Summary

PIA is a **private, personal AI agent** that runs for exactly one user — Nitish, a final-year student. It continuously reads a small set of **selected WhatsApp placement/academic groups** through a self-hosted Evolution API connector and turns the noisy chat stream into personally relevant, persistent knowledge.

Concretely, PIA:

1. **Detects company eligibility lists** delivered as plain text, Excel/CSV files, PDFs, or images/screenshots, and checks whether Nitish's name / roll number appears in them using a deterministic-first matching ladder (FR-ELG-*).
2. **Remembers which companies he is eligible for** — once eligibility is established, every later mention of that company is automatically prioritized and linked to that memory (FR-MEM-*).
3. **Extracts deadlines and placement events** — registration windows, online assessments (OA), interviews, KYC sessions, venues, forms — as first-class, timezone-normalized objects (FR-EVT-*).
4. **Suppresses duplicate and reminder spam** — repeated or semantically identical announcements produce one canonical event and one notification, while genuine new information (a changed time, a new link) still alerts (FR-DED-*, FR-EVT-004).
5. **Surfaces important non-placement academic/administrative notices** — domain classification is independent of placement filtering (FR-CLS-001/002).
6. **Delivers every alert with evidence** — priority, reason, and a link to the source message and document; observed facts are always distinguished from AI interpretation (FR-NOT-007, SEC-009).

The product is deliberately a **hybrid deterministic + AI platform**: code owns identity, timestamps, persistence, state transitions, deduplication, notification history, and safety; LLMs only interpret, classify, and extract, and their structured output is validated before it can influence authoritative state.

**Scope of MVP:** read-and-notify only. PIA monitors WhatsApp groups **read-only** (no sends, no replies — DEC-003), on a **secondary WhatsApp number** (recommended; ban-risk mitigation), and pushes alerts to a **Telegram bot** (DEC-002). No autonomous form submission, no KYC automation (excluded permanently — DEC-008), no conversational search or browser automation in MVP (P12–P14 out of MVP per DEC-004).

**Primary outcome:** *"Tell me what matters to me, remember the context, and do not make me read the same thing twice."* Nitish should not need to read the placement groups continuously.

---

## 2. Problem Statement

Placement-season WhatsApp groups are the de-facto official channel for campus recruiting, but they are hostile to the one person who most needs their content:

| Problem | Today's experience | Consequence |
|---|---|---|
| **Volume & noise** | Hundreds of messages/day, most greetings, memes, forwards, and chatter | Important announcements scroll away within minutes |
| **Eligibility lists in hostile formats** | Company "eligible candidates" lists arrive as Excel files, PDFs, or screenshots | Nitish must download and search each list manually, on a phone, hoping to spot his name |
| **No memory** | "Accenture shortlist posted" means nothing unless he remembers he was on Accenture's eligibility list | Missed OA/interview rounds for companies where he was already eligible |
| **Duplicates** | The same deadline is re-posted 3–5 times by admins | Repeated reading and alert fatigue (or, worse, desensitization) |
| **Deadlines in messy language** | "Register today 6 PM", "OA tomorrow 10 AM sharp" | Missed registrations and assessments that end careers at those companies |
| **Mixed domains** | Exam schedules, fee deadlines, and admin notices appear in the same groups | Non-placement but critical items get lost when filtering for placement only |

The cost of a single miss is concrete: an unnoticed eligibility list → an unnoticed OA deadline → a lost offer opportunity. PIA exists to make that cost structural rather than behavioral — the system watches, remembers, and reminds, so Nitish doesn't have to.

---

## 3. Persona & User Context

**Nitish — final-year student, single user of a personal tool.** PIA is *not* a multi-tenant SaaS product; there is one profile, one dashboard, one notification channel.

Data Nitish prepares upfront (from the leaner source doc §6):

| Data | Used for |
|---|---|
| Full name **as it might appear in college lists**, plus common misspellings/variants | Identity aliases (FR-PRO-002) |
| Roll number / registration number / student ID | Identifier-first matching (FR-ELG-006) |
| CGPA, branch, batch, 10th/12th %, backlog status | Profile attributes; future form-preparation (out of MVP) |
| Resume / certificates / academic proofs (optional) | Profile documents (FR-PRO-003) |
| A Telegram account for the bot to message him | Notification channel (DEC-002) |
| A **secondary WhatsApp number** (recommended) linked to Evolution API | Read-only group monitoring with minimized ban risk (DEC-003) |

---

## 4. Goals and Non-Goals

### 4.1 Goals (from master §2.1, adapted)

- G-1 — Capture messages and media from **selected** WhatsApp groups only, via a replaceable connector layer (FR-WA-*; ADR-001).
- G-2 — Understand message intent; classify domain (placement / academic / examination / administrative / event / general) and importance independently (FR-CLS-001/002).
- G-3 — Detect eligible-candidate announcements and analyze attached Excel/PDF/image lists (FR-ELG-001..004).
- G-4 — Match Nitish's profile with exact, normalized, identifier, and controlled fuzzy matching (FR-ELG-005..007).
- G-5 — Persist company eligibility and build company/application lifecycle memory (FR-MEM-*).
- G-6 — Extract deadlines, exam dates, interview schedules, links, forms, KYC sessions, venues, and required actions (FR-EVT-001/002).
- G-7 — Detect repeated or semantically duplicate information; notify only on genuinely new information (FR-DED-*).
- G-8 — Provide concise, evidence-backed notifications and an auditable dashboard/history (FR-NOT-007).
- G-9 — (Later phases) Support human-approved actions (form preparation) with permissioning and audit — **specified now, not built in MVP**.

### 4.2 Non-Goals for MVP (master §2.2 + DEC-003/004/008)

- **Autonomous form submission or any irreversible external action** — disabled by default (SEC-005); `ACTION_AUTOMATION_ENABLED=false`.
- **Processing every personal WhatsApp conversation** — only allowlisted groups are processed (FR-WA-004, SEC-006).
- **Publishing or sharing group content with third parties.**
- **Using an LLM as the sole source of truth** for candidate identity or eligibility (ADR-004/005).
- **Building a general-purpose autonomous browsing agent** before the core information pipeline is reliable.
- **Outbound WhatsApp activity of any kind** — no sends, replies, or reactions via the connector (DEC-003, ban-risk mitigation).
- **KYC attendance/submission automation — permanently excluded from the product**, not just MVP: a KYC session exists to verify identity, and automating it would misrepresent Nitish's identity to companies/college (DEC-008). KYC *sessions are still detected and reminded* as events (FR-EVT-001).
- **P12–P14 features in MVP**: conversational search (P12), action preparation (P13), controlled automation (P14) — future scope only (DEC-004).

---

## 5. Success Metrics

User-measurable outcomes, drawn from master §14 core metrics and NFR-001..009 targets:

| # | Metric | Target | Source |
|---|---|---|---|
| SM-1 | Message reliability — no silent loss after webhook acceptance | ≥ 99.5% processed **or visibly failed/retrying** | NFR-001 |
| SM-2 | Duplicate provider deliveries create zero duplicate records | 100% for tested duplicate cases | NFR-002 |
| SM-3 | Normal text messages searchable | p95 < 10 s under normal load | NFR-003 |
| SM-4 | Standard media/documents processed | p95 < 2 min, configurable | NFR-004 |
| SM-5 | No duplicate alerts for the same canonical event without material delta | ≥ 99% in regression suite | NFR-005 |
| SM-6 | Ambiguous identity never auto-confirmed as ELIGIBLE | 100% in safety tests | NFR-006 |
| SM-7 | Critical/high notifications traceable to evidence | 100% | NFR-007 |
| SM-8 | Eligibility detection coverage across formats | Known fixtures correctly matched for XLSX, CSV, PDF, image | MVP gate §26 |
| SM-9 | Zero credentials/secrets in logs or source control | Continuous secret scanning clean | NFR-008 |
| SM-10 | Deadline reminders delivered before due time | 100% of scheduled reminders on time | §14 metrics |

Operational metrics tracked from day one (master §14): messages received; processing lag; attachment success rate; classifier confidence distribution; eligibility match rate; false-match corrections; duplicate suppression count; notifications sent; delivery failures; deadline reminders delivered on time.

---

## 6. User Stories

All nine user stories from master §3, expanded into Given/When/Then acceptance criteria.

### US-01 — Connect WhatsApp groups
*As a student, I want to connect selected WhatsApp groups so I do not need to manually copy messages.*
- **Given** the Evolution API instance is paired via QR with the (recommended secondary) number, **when** Nitish selects groups in the dashboard, **then** messages and media from those groups arrive reliably and **exactly once** (FR-WA-001..004, FR-MSG-002).
- **Accept:** a test message in an allowed group appears in PIA within seconds; a message in a non-selected group never reaches the AI pipeline (SEC-006).

### US-02 — Understand what is important
*As a student, I want the agent to understand what is important.*
- **Given** a stream of mixed chatter and announcements, **when** messages are processed, **then** noise is suppressed and high-value announcements are surfaced (FR-CLS-001/002, FR-NOT-001).
- **Accept:** greetings/memes classify as IGNORE and never notify; a placement announcement classifies CRITICAL/HIGH.

### US-03 — Automatic eligibility-list checking
*As a student, I want eligible-candidate files checked against my profile automatically.*
- **Given** an eligibility list arrives as text, XLSX/CSV, PDF, or image, **when** it is processed, **then** PIA tells Nitish whether his identity was found **and shows the evidence** (FR-ELG-001..008).
- **Accept:** the alert contains match method, confidence, and a reference to the exact sheet/page/row where the match was found.

### US-04 — Company memory
*As a student, I want the agent to remember that I am eligible for a company.*
- **Given** an eligibility record exists for a company, **when** any later message mentions that company (or an alias), **then** it is automatically linked and prioritized (FR-MEM-003/005).
- **Accept:** no manual re-subscription is needed; "Accenture OA" resolves to the existing Accenture company record.

### US-05 — Deadlines detected and surfaced
*As a student, I want deadlines detected and surfaced.*
- **Given** a message contains a deadline in absolute or relative language ("today 6 PM", "tomorrow", "12 Sep"), **when** processed, **then** a timezone-normalized deadline object is created and timely personalized alerts are scheduled (FR-EVT-002/003, FR-NOT-005).
- **Accept:** stored timestamps carry timezone (Asia/Kolkata); missing dates remain unknown — never invented (FR-EVT-005).

### US-06 — No repeated alerts
*As a student, I do not want repeated messages to produce repeated alerts.*
- **Given** the same announcement is re-posted (exact, near-identical, or semantically identical), **when** processed, **then** it maps to the existing canonical event and no new notification fires (FR-DED-001..003, ADR-006).
- **Accept:** three reminders about the same deadline produce exactly one canonical event; a material delta (new link/venue/time) still triggers one update notification (FR-DED-004).

### US-07 — Important academic updates still surface
*As a student, I still want important academic updates from placement groups.*
- **Given** a non-placement notice (exam schedule, fee deadline) appears in a placement group, **when** classified, **then** domain is independent from placement-only filtering and a CRITICAL academic deadline is surfaced (FR-CLS-002).

### US-08 — Evidence behind every conclusion
*As a student, I want evidence behind every important conclusion.*
- **Given** any important notification, **when** Nitish opens it, **then** it links to the source group message and document, and extraction/match reasoning is inspectable (FR-NOT-007, SEC-009).

### US-09 — Permissioned future automation
*As a student, I want future automation to use only the information and capabilities I explicitly authorize.*
- **Given** a future action proposal (P13+, out of MVP), **when** created, **then** it is permissioned, auditable, and approval-gated — nothing executes silently (SEC-004/005, ADR-008).

---

## 7. Functional Requirements

Priorities: **MUST** = required for MVP release gate; **SHOULD** = required if declared, degrade gracefully otherwise. IDs are stable — never renumber or rename (referenced by docs 02–06).

### 7.1 WhatsApp Connection & Group Registry (FR-WA-*)

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| FR-WA-001 | MUST | Connect WhatsApp account through Evolution API instance; user can pair and see connection state; reconnect failures surfaced. | Connection state transitions are persisted and visible. |
| FR-WA-002 | MUST | Receive group messages as normalized internal events (provider payload → stable internal schema). | Same provider event reprocessed twice creates one logical message. |
| FR-WA-003 | MUST | Persist group metadata (group ID, name, participant metadata where provider supplies it). | Group registry can identify a message source. |
| FR-WA-004 | MUST | Group allowlist/denylist — only selected groups are processed by default. | Unselected groups are never sent to the AI pipeline. |
| FR-WA-005 | SHOULD | Classify groups into Placement/Academic/Administrative/General/Other; manual override supported. | Classification can be edited without modifying raw messages. |
| FR-WA-006 | MUST | Receive message attachments/media metadata and fetch content when permitted; failed downloads become retryable jobs, not silent drops. | Failed media downloads are visible, retryable jobs. |

### 7.2 Message Normalization & Idempotency (FR-MSG-*)

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| FR-MSG-001 | MUST | Normalize message ID, sender, group, timestamp, text, media references into a canonical provider-independent schema. | Unit tests cover missing/optional provider fields. |
| FR-MSG-002 | MUST | Idempotent ingestion — duplicate webhook/event creates no duplicate record or job. | 10 identical deliveries result in one stored logical event. |
| FR-MSG-003 | SHOULD | Preserve raw provider payload securely for audit/debugging with configurable retention. | Raw data linkable to normalized message; retention configurable. |
| FR-MSG-004 | MUST | Track processing states: RECEIVED, QUEUED, PROCESSING, PROCESSED, FAILED, RETRYING. | State transitions are queryable. |

### 7.3 Message Classification (FR-CLS-*)

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| FR-CLS-001 | MUST | Classify domain: PLACEMENT, ACADEMIC, EXAMINATION, ADMINISTRATIVE, EVENT, GENERAL, UNKNOWN — with confidence/evidence. | Low-confidence cases are not silently treated as high importance. |
| FR-CLS-002 | MUST | Classify importance independently: CRITICAL, HIGH, MEDIUM, LOW, IGNORE. | A non-placement academic deadline can be CRITICAL. |
| FR-CLS-003 | MUST | Extract company names, institutions, dates, links, locations, actions, deadlines, document references into a structured schema. | Unknown fields are null, not hallucinated. |
| FR-CLS-004 | SHOULD | Rule-based overrides for known patterns; overrides are auditable. | User/system rules can override model classification. |

### 7.4 Candidate Profile & Personal Knowledge (FR-PRO-*)

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| FR-PRO-001 | MUST | Secure candidate profile storing identity and eligibility attributes; profile changes audited. | Profile changes appear in the audit log. |
| FR-PRO-002 | MUST | Identity aliases and identifiers: name variants, roll number, registration number, student ID, approved aliases. | Matcher can use all configured identifiers. |
| FR-PRO-003 | SHOULD | Profile documents (resume, certificates, academic proofs) with explicit access permissions. | Documents have explicit access permissions. |
| FR-PRO-004 | MUST | User can correct a false match or false negative; correction becomes supervised feedback stored with timestamp and source. | Correction is stored, auditable, and affects future matching. |

### 7.5 Eligibility List Intelligence (FR-ELG-*)

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| FR-ELG-001 | MUST | Detect that a message/attachment represents an eligible-candidate list (text, file name, document content signals). | Tested on text-only, file-only, and combined announcements. |
| FR-ELG-002 | MUST | Parse XLSX/XLS/CSV lists: extract rows/columns and likely identity fields; support common header variations. | Normalized candidate rows extracted for fixture files. |
| FR-ELG-003 | MUST | Parse PDFs: extract text with page/position evidence where feasible. | User match includes page reference. |
| FR-ELG-004 | MUST | Analyze images/screenshots: OCR first; vision model when OCR is insufficient; match includes image/page evidence. | Match evidence references the image region/page. |
| FR-ELG-005 | MUST | Normalize candidate names before matching (case, whitespace, punctuation, Unicode, common formatting variations). | Exact normalized matches work reliably. |
| FR-ELG-006 | MUST | Multiple identity signals: prefer roll/registration ID when available; name as primary/secondary depending on data. | Ambiguous same-name cases are never auto-confirmed. |
| FR-ELG-007 | SHOULD | Controlled fuzzy matching — only after deterministic checks, returning confidence plus evidence; configurable threshold; ambiguous matches require review. | Threshold is configurable; ambiguous → review state. |
| FR-ELG-008 | MUST | Create Eligibility Record when user is found: company + candidate + source + confidence + detected_at, linked to original message/document. | Record links to its source message/document. |
| FR-ELG-009 | MUST | Create Not Found / Unknown state **without claiming ineligibility** — absence from one document is not rejection unless document semantics support it. | System distinguishes NOT_FOUND from NOT_ELIGIBLE. |

### 7.6 Company Memory & Lifecycle (FR-MEM-*)

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| FR-MEM-001 | MUST | Create company entity from messages: canonical name + aliases. | "Accenture"/"Accenture India"-style variants map to one entity when evidence supports it. |
| FR-MEM-002 | MUST | Persist company-specific eligibility state: ELIGIBLE, NOT_ELIGIBLE, UNKNOWN, EXPIRED, SUPERSEDED — state changes keep source evidence. | State changes retain source evidence. |
| FR-MEM-003 | MUST | Associate later messages with known companies (mention, aliases, event/group context). | "Accenture OA" resolves to the existing company record. |
| FR-MEM-004 | SHOULD | Track application lifecycle: DISCOVERED → ELIGIBLE → REGISTRATION → OA → SHORTLISTED → INTERVIEW → SELECTED/REJECTED; transitions event-driven and reversible via correction. | Transitions recorded with evidence; corrections reverse states. |
| FR-MEM-005 | MUST | Company-specific watch state — once eligible, future related messages are prioritized automatically, no manual re-subscription. | Eligible-company updates route to high priority without user action. |

### 7.7 Event & Deadline Engine (FR-EVT-*)

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| FR-EVT-001 | MUST | Extract event types: REGISTRATION, FORM, KYC, OA, EXAM, INTERVIEW, SHORTLIST, RESULT, VENUE, DOCUMENT_SUBMISSION, JOINING, OTHER. | Structured event record created per type. |
| FR-EVT-002 | MUST | Extract absolute and relative dates/times ("today 6 PM", "tomorrow") resolved in the user timezone; stored timestamps include timezone. | Relative dates resolve correctly against Asia/Kolkata. |
| FR-EVT-003 | MUST | Track deadlines as first-class objects: OPEN, DUE_SOON, EXPIRED, CANCELLED, COMPLETED; updatable by later messages. | Deadline can be updated by a later message. |
| FR-EVT-004 | MUST | Detect event deltas — new link, changed deadline, new venue, changed time update the existing event; user notified only for meaningful deltas. | Only the delta notifies, not the whole re-announcement. |
| FR-EVT-005 | MUST | Prevent hallucinated deadlines — missing date/time remains unknown. | Model cannot invent a deadline. |

### 7.8 Semantic Deduplication & Information Delta (FR-DED-*)

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| FR-DED-001 | MUST | Detect exact duplicate messages (content hash + provider ID); duplicates do not re-notify. | Duplicate does not re-notify. |
| FR-DED-002 | MUST | Detect near-duplicate messages via normalized textual similarity; minor wording changes map to the same candidate event when appropriate. | Reworded announcement maps to same event. |
| FR-DED-003 | MUST | Detect semantic duplicates by comparing event/company/action/deadline facts, not only raw text. | Three reminders about the same deadline produce one canonical event. |
| FR-DED-004 | MUST | Detect information delta — same event with new link/venue/deadline/status updates the event; new material information can trigger a notification. | Delta updates event and notifies once. |
| FR-DED-005 | SHOULD | Maintain canonical event history — every update links to source messages; timeline shows original and updates. | Timeline shows full update chain. |

### 7.9 Personal Relevance & Notification (FR-NOT-*)

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| FR-NOT-001 | MUST | Calculate personal relevance using profile, eligibility, company memory, academic context, role, and actionability. | Relevant company update is prioritized. |
| FR-NOT-002 | MUST | Notify eligible-company updates — any new high-value event for a company where the user is eligible is considered for immediate notification. | Accenture eligibility followed by OA deadline produces an alert. |
| FR-NOT-003 | MUST | Notify critical academic/admin information even with no company relation. | Academic exam deadline is surfaced from a placement group. |
| FR-NOT-004 | MUST | Suppress already-delivered identical information using notification history. | Repeated reminder does not spam the user. |
| FR-NOT-005 | SHOULD | Escalation — upcoming deadlines become more urgent as due time approaches; configurable reminder windows. | Reminder schedule fires as configured. |
| FR-NOT-006 | SHOULD | Daily digest grouping urgent, placement, academic, and tracked items — only new/actionable information. | Digest contains no already-delivered items. |
| FR-NOT-007 | MUST | Include evidence in alerts: source group/message and attachment when relevant; user can trace the alert to its origin. | 100% of critical/high alerts carry evidence. |

**Delivery channel note (DEC-002):** requirements above are channel-agnostic. MVP implements the Telegram Bot adapter as the primary channel; email and WhatsApp-self adapters are interface-only. There are **no outbound WhatsApp sends** in MVP (DEC-003).

### 7.10 Security, Privacy & Safety Requirements (SEC-*)

| ID | Priority | Requirement | Acceptance criteria |
|---|---|---|---|
| SEC-001 | MUST | All secrets, WhatsApp session credentials and API tokens excluded from source control. | Control enforced, covered by automated tests (secret scanning). |
| SEC-002 | MUST | Sensitive profile fields and stored documents encrypted at rest where practical, access-controlled. | Control enforced, covered by automated tests. |
| SEC-003 | MUST | Webhook requests authenticated/verified and rate-limited. | Control enforced, covered by automated tests. |
| SEC-004 | MUST | Every future external action permission-checked and audited. | Control enforced, covered by automated tests. |
| SEC-005 | MUST | Autonomous irreversible actions disabled by default. | Control enforced, covered by automated tests. |
| SEC-006 | MUST | Processing minimized to enabled groups only. | Control enforced, covered by automated tests. |
| SEC-007 | SHOULD | Retention policy exists for raw media, messages, and model artifacts. | Control enforced, covered by automated tests. |
| SEC-008 | SHOULD | Model providers configurable so privacy-sensitive deployments can choose an appropriate provider. | Control enforced, covered by automated tests. |
| SEC-009 | MUST | User-facing evidence distinguishes observed facts from AI interpretation. | Control enforced, covered by automated tests. |

Concrete engineering mechanisms for each control are specified in 02_TRD §9.

### 7.11 Non-Functional Requirements (NFR-*)

| ID | Requirement | Target |
|---|---|---|
| NFR-001 | Reliability — no silent message loss after webhook acceptance | ≥ 99.5% processed or visible in failed/retry state |
| NFR-002 | Idempotency — duplicate provider deliveries create no duplicate logical records | 100% for tested duplicate cases |
| NFR-003 | Timeliness — normal text messages become searchable quickly | p95 < 10 s under normal load |
| NFR-004 | Media processing — common documents complete asynchronously | p95 < 2 min for standard files, configurable |
| NFR-005 | Notification accuracy — no duplicate alerts for the same canonical event without delta | ≥ 99% in regression suite |
| NFR-006 | Match safety — ambiguous identity never auto-confirmed | 100% in safety tests |
| NFR-007 | Auditability — important notifications trace to evidence | 100% of critical/high notifications |
| NFR-008 | Security — no credentials in logs/source | Continuous secret scanning + tests |
| NFR-009 | Extensibility — provider adapter replaceable without rewriting the business engine | Interface contract + integration tests |

Design mechanisms meeting each target are mapped in 02_TRD §12.

---

## 8. Notification Policy (master §11)

| Priority | Examples | Default delivery | Dedup rule |
|---|---|---|---|
| **CRITICAL** | Deadline today; mandatory KYC; exam/reporting time; user confirmed eligible and action required | Immediate | No repeat unless material delta or escalation window |
| **HIGH** | OA announcement; shortlist; new company event; registration deadline > 24h | Immediate | One notification per canonical event state |
| **MEDIUM** | General academic announcement; non-urgent update | Daily digest | Aggregate by topic/event |
| **LOW** | Contextual or informational message | Digest / dashboard | Aggregate |
| **IGNORE** | Greetings, memes, casual chatter, duplicate reminders | No notification | Always suppress unless user explicitly asks |

### Notification template contract (master §11.1)

Every alert renders:

1. **Title:** `[priority emoji] [canonical event/company]`
2. **What changed:** one or two sentences
3. **Why it matters to you:** explicit relevance reason
4. **Deadline/time:** exact local date/time if known
5. **Action:** what the user should do
6. **Source:** group + message/document reference
7. **Confidence:** optional — only when ambiguity exists

---

## 9. MVP Scope

### 9.1 In scope (P0–P11 per DEC-004)

| Phase | Name | Summary of capability delivered |
|---|---|---|
| P0 | Architecture & repository foundation | Docker, env management, repo structure, coding standards, test harness, observability baseline |
| P1 | Evolution API connector | Pair WhatsApp (read-only), receive webhooks, group discovery, connection health |
| P2 | Message persistence | Normalize message schema, attachments, idempotency, processing states |
| P3 | Basic intelligence | Domain/importance classification + entity extraction (rule + LLM hybrid) |
| P4 | Candidate profile | Secure profile, aliases, identity fields, corrections |
| P5 | Document intelligence | XLSX/CSV/PDF/image pipeline with evidence extraction |
| P6 | Eligibility engine | Matching ladder, confidence, ambiguity handling, eligibility records |
| P7 | Company memory | Company entities, aliases, watch state, lifecycle |
| P8 | Event/deadline engine | Event canonicalization, deadline extraction, delta updates |
| P9 | Semantic dedup | Exact, near, semantic duplicates; delta detection; pHash (DEC-009) |
| P10 | Notification engine | Priority, immediate/digest, escalation, delivery history, Telegram delivery (DEC-002) |
| P11 | Dashboard | Overview, companies, timeline, inbox, documents, notifications, profile, settings, audit |

### 9.2 Explicitly out of scope for MVP

- P12 conversational search ("what happened with Accenture?")
- P13 action preparation (KYC/form task drafting, approval workflow)
- P14 controlled automation (browser/form execution)
- Any outbound WhatsApp activity (DEC-003)
- KYC attendance/submission automation — **permanently excluded** (DEC-008)
- Email notification channel (interface defined, not built — DEC-002)
- Multi-user/multi-tenant operation

### 9.3 MVP Release Gate (master §26)

- [ ] At least one real/fixture WhatsApp group successfully ingested end-to-end
- [ ] At least one XLSX, one PDF, and one image eligibility list processed
- [ ] Correct user match achieved for known fixtures
- [ ] Ambiguous same-name fixture never auto-confirms eligibility
- [ ] Company memory links later company updates correctly
- [ ] At least one deadline extracted and normalized to Asia/Kolkata timezone
- [ ] Repeated reminder sequence produces one notification; a material delta produces one update
- [ ] Important academic announcement surfaced even if placement classification is false
- [ ] Every critical/high alert has evidence
- [ ] Failure/retry paths observable
- [ ] Secrets and sensitive data absent from logs/source control
- [ ] Automated action capability remains disabled in MVP

---

## 10. Risks & Mitigations

| Risk | Impact | Mitigation | Source |
|---|---|---|---|
| WhatsApp account flagged/banned (unofficial Baileys protocol) | High | Read-only monitoring only; no sends; silence health-check; **owner decision 2026-09-05: primary number accepted by Nitish — secondary number remains the safer recommendation, revisit on any warning sign** | Leaner §2/§7, DEC-003 update |
| False identity match (wrong student told they're eligible) | Critical | Deterministic identity signals first; AMBIGUOUS state never auto-confirms; evidence required; human correction loop (FR-PRO-004) | Master §23, ADR-005 |
| OCR misreads names on image lists | High | Layered OCR + vision fallback; source image retained; confidence always shown; review state for ambiguous | Master §23, leaner §7 |
| Hallucinated deadline | Critical | Structured extraction with null-when-unknown policy; source evidence; validator layer (FR-EVT-005, ADR-004) | Master §23 |
| Notification spam | High | Canonical events (ADR-006), semantic dedup, delivery history, digest | Master §23 |
| Missed urgent update | High | High recall for critical patterns; escalation windows (FR-NOT-005); regression corpus; monitoring | Master §23 |
| Connector changes/failure | High | Adapter boundary (ADR-001), health checks, replay/idempotency, backup notification path | Master §23 |
| Sensitive data leakage | Critical | Encryption at rest for sensitive fields/documents; least privilege; secret management; audit log | Master §23, SEC-002 |
| Unapproved form submission | Critical | Actions disabled by default (SEC-005); explicit approval; allowlists; audit trail | Master §23 |
| Vendor/provider lock-in | Medium | Provider abstraction for messaging and LLMs (ADR-001, SEC-008) | Master §23 |
| Evolution API license changes | Medium | Pin tested version; monitor upstream LICENSE/TRADEMARKS; maintain attribution/usage notice | Master §23/§27 |
| Silence mistaken for "no updates" | Medium | "Last message received at" health signal — silence is noticed, not assumed benign | Leaner §7 |

---

## 11. Assumptions & Open Questions

Assumptions carried until the user answers (from Master Plan §7):

| # | Open question | Working default |
|---|---|---|
| Q1 | LLM provider, model tier, monthly budget? | Anthropic Claude default; OpenAI-compatible fallback configurable (DEC-006) |
| Q2 | Hosting: VPS vs home server, budget/region? | Small VPS; `APP_TIMEZONE=Asia/Kolkata` |
| Q3 | Dashboard auth for single-user deployment? | Yes — minimal single-credential/token auth anyway (SEC-002 posture) |
| Q4 | Evolution API version pin + exact webhook payload schema? | Pin latest stable at P1 start; verify license before distribution |
| Q5 | Secondary WhatsApp number available? Which groups? | Secondary number recommended; allowlist configured at onboarding |
| Q6 | Telegram bot token confirmed? Email fallback needed in MVP? | Telegram only in MVP |
| Q7 | Retention defaults (raw messages, documents, media size cap)? | Sensible personal defaults proposed in 02_TRD §9; configurable |
| Q8 | Evaluation data source for fuzzy/ambiguous thresholds (ADR-009)? | Fixture corpus built in P5/P6; thresholds tuned and documented there |

> **Resolved 2026-09-05 (owner answers):** Q1 = **NVIDIA NIM hosted API** (OpenAI-compatible; `meta/llama-3.3-70b-instruct` + `meta/llama-3.2-90b-vision-instruct`); Q2 = **cheap always-on VPS**; Q5 = **primary WhatsApp number** (owner-accepted risk — mitigations enforced, see DEC-003 update); Q6 = **Telegram confirmed**. Remaining open with defaults: Q3 (single bearer token), Q4 (pin Evolution version at P1), Q7 (retention defaults), Q8 (thresholds via P5/P6 fixtures).

---

## 12. Traceability

| This PRD section | Traces to |
|---|---|
| §1 Executive summary | Master §1; leaner §1; DEC-002/003/004/008 |
| §2 Problem statement | Master §1, leaner §1 |
| §3 Persona | Leaner §6; FR-PRO-* |
| §4 Goals / non-goals | Master §2; DEC-003/004/008 |
| §5 Success metrics | Master §14, NFR-001..009 |
| §6 User stories | Master §3 (US-01..09) |
| §7 Functional requirements | Master §7 (FR-WA/MSG/CLS/PRO/ELG/MEM/EVT/DED/NOT) |
| §8 Notification policy | Master §11 |
| §9 MVP scope & release gate | Master §18/§26; DEC-004 |
| §10 Risks | Master §23; leaner §7 |
| §11 Open questions | Master Plan §7 |

*Consumed by: 02_TRD (engineering), 03_App_Flow (runtime behavior), 04_UIUX_Brief (screens), 05_Backend_Schema (tables), 06_Implementation_Plan (build sequence).*
