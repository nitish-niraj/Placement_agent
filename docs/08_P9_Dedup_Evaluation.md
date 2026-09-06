# 08 — P9 Dedup Evaluation Record (FR-DED-*, DEC-009)

| | |
|---|---|
| Project | **Placement Intelligence Agent (PIA)** |
| Milestone | P9 — Semantic dedup (F-022) |
| Date | 2026-09-06 |
| Governs | `DEDUP_NEAR_THRESHOLD`, `DEDUP_NEAR_WINDOW_HOURS`, `DEDUP_EXACT_REMINDER_DAYS` |
| Corpus | all 33 captured text messages (>80 chars) of the enabled groups, live 2026-09-06 |

## 1. Layer design (TRD §7, cheap-to-expensive)

| Layer | Mechanism | Where |
|---|---|---|
| Exact (FR-DED-001) | `content_hash` equality within the reminder window; the same content after ≥ 7 days is a **legitimate reminder** and is never suppressed (DEC-009) | API ingestion |
| Visual (DEC-009) | 64-bit dHash at ingestion; suppression at Hamming distance ≤ 4 bits within the near window | API ingestion |
| Near (FR-DED-002) | token-Jaccard ≥ threshold, **gated on company agreement** | API ingestion |
| Semantic (FR-DED-003/004) | canonical fact tuple + near-match delta | P8 event layer (records.py) |

Suppressed copies are persisted as PROCESSED with a `classification.status =
"duplicate"` annotation, audited (`dedup.suppressed` / `event.duplicate`), and
counted in the `duplicate_suppressed_total{layer}` Redis counter. They never
reach classification, parsing, or notifications.

## 2. Measured corpus (token-Jaccard, same group)

**Same-company pairs — suppression candidates:**

| Similarity | Pair |
|---:|---|
| 1.000 | verbatim re-sends (also caught by the exact layer) |
| 0.795 | "informed via Mail" vs "shortlisted" (MOVIDU) |
| 0.720 | "*Gentle Reminder*" vs original eligibility announcement (same company) |

**Different-company pairs — must NOT suppress:** the announcement template is
shared, so reworded-but-different companies score up to **0.705**
("*Gentle Reminder*" of company A vs company B). This is why a naive
text-similarity threshold is unsafe and the near layer carries a
**company-agreement gate** (company keys from the `companies` table matched
inside both texts; when neither side has any company signal the bar is
`threshold + 0.05`).

## 3. Chosen values

| Setting | Value | Position in the empirical gap |
|---|---:|---|
| `DEDUP_NEAR_THRESHOLD` | **0.72** | same-company min 0.720 (inclusive), different-company max 0.705 — inside the 0.705–0.720 gap, guarded additionally by the company gate |
| `DEDUP_NEAR_WINDOW_HOURS` | **48** | placement announcements are re-sent within hours; DEC-009's 7-day rule already protects older exact repeats |
| `DEDUP_EXACT_REMINDER_DAYS` | **7** | DEC-009: identical content after ~7 days is a legitimate reminder |

Error trade-off: suppression failure (a repeat slips through) costs a later
semantic-dedup pass at the event layer plus notification dedup in P10
(ADR-006 canonical events are the last line). Over-suppression is guarded by
the company gate and the 7-day reminder rule — the product never loses an
announcement, it only delays repeats.

## 4. Re-running

The similarity survey is a read-only query over live messages (token-Jaccard of
same-group pairs, labeled by shared company keys) — the values above document
the 2026-09-06 corpus; re-run after significant new captures and adjust
`DEDUP_NEAR_THRESHOLD` with this document updated in the same change.
