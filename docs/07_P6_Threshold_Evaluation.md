# 07 — P6 Threshold Evaluation Record (ADR-009 / Open Question Q8)

| | |
|---|---|
| Project | **Placement Intelligence Agent (PIA)** |
| Milestone | P6 — Eligibility Engine (F-015) |
| Date | 2026-09-05 |
| Governs | `FUZZY_MATCH_THRESHOLD`, `AMBIGUOUS_MATCH_THRESHOLD` (02_TRD §6, master §22) |
| Corpus | `tests/fixtures/eligibility_corpus.py` · Guard test: `tests/unit/test_threshold_corpus.py` |

ADR-009 forbids invented constants: the two matching thresholds are derived from a
labeled fixture corpus, recorded here with the full measurement table and the accepted
error trade-off. Any corpus change requires re-running the guard tests and updating
thresholds + this document together.

---

## 1. What the thresholds govern (semantics)

Stage 4 (FUZZY) of the identity ladder runs **only after** the deterministic stages
EXACT / NORMALIZED / IDENTIFIER produced no decision — i.e. the row carries no
comparable identifier and its name is not equal to any profile name. The similarity
`sim = max(full-string ratio, token-set ratio)` over all profile names then selects a band:

| Band | Condition | Outcome |
|---|---|---|
| Fuzzy hit | `sim >= FUZZY_MATCH_THRESHOLD` | **AMBIGUOUS**, method FUZZY, `requires_user_review=true` — a fuzzy-only hit NEVER auto-matches (02_TRD §6 governance; ADR-005) |
| Review band | `AMBIGUOUS_MATCH_THRESHOLD <= sim < FUZZY` | **AMBIGUOUS**, method MODEL_REVIEW — proposal for human/assisted reading, never ELIGIBLE |
| Ignore | `sim < AMBIGUOUS_MATCH_THRESHOLD` | **NOT_FOUND** (which is not NOT_ELIGIBLE, FR-ELG-009) |

Both thresholds therefore separate *plausibly-me* names from *different-person* names;
they never alone produce ELIGIBLE. Identifier evidence is the only auto-ELIGIBLE path
when names are ambiguous (identifier dominance, `matcher.py`).

**Similarity metric** (`pia_worker.eligibility.matcher.name_similarity`):
`max(SequenceMatcher(a, b), token-set variants)` on normalized names — token-set handles
token order ("kumar nitish") and subset names ("nitish kumar singh"); the full-string
ratio catches character-level typos. Stdlib `difflib`, deterministic, no dependencies.

## 2. Measurement table

Similarity of each corpus row against the profile's canonical name `nitish kumar`
(normalized). Labels were assigned by human judgment **before** thresholds were chosen.

| Row (as printed) | sim | Label | Note |
|---|---:|---|---|
| Kumar Nitish | 1.000 | me | reversed token order |
| Nitish Kummar | 0.960 | me | transposed chars in surname |
| Nithish Kumar | 0.960 | me | doubled-h spelling variant |
| Kumaar Nitish | 0.960 | me | order + vowel stretch (vs alias "nitish kumaar") |
| NITISHKUMAR. | 0.957 | me | no space + trailing dot |
| Nitish Kumar Singh | 1.000 | could_be_me | **REAL case (reg 12528230)** — name-only similarity cannot exclude him; the identifier stage decides NOT_FOUND before fuzzy is consulted |
| Nitish Kumari | 0.960 | could_be_me | shared tokens over-include; accepted review noise |
| Nitesh Kumar | 0.917 | could_be_me | first-name vowel variant: typo or another student |
| Kumar Nitin | 0.870 | not_me | shared tokens, different person |
| Nisha Kumari | 0.833 | not_me | same surname pattern, different first name |
| Nitish Ranjan | 0.667 | not_me | shared first name, different surname |
| Amit Verma | 0.455 | not_me | unrelated |
| Priya Singh | 0.348 | not_me | unrelated |
| Rahul Sharma | 0.333 | not_me | unrelated |

Empirical separation points: **min "me" = 0.957**, **min "could_be_me" = 0.917**,
**max "not_me" = 0.870**. Names whose normalized form equals a profile alias are
decided at stage 1/2 and are deliberately excluded (their similarity says nothing
about the fuzzy bands).

## 3. Chosen values and margins

| Setting | Value | Position in the empirical gap |
|---|---:|---|
| `FUZZY_MATCH_THRESHOLD` | **0.95** | just under min "me" (0.957): every owner variant is at least review-worthy; "Nitesh Kumar" (0.917) stays below → MODEL_REVIEW band |
| `AMBIGUOUS_MATCH_THRESHOLD` | **0.90** | inside the 0.870–0.917 gap: 0.030 above the closest different-person name ("Kumar Nitin"), 0.017 under the closest could-be-me name ("Nitesh Kumar") |

Defaults live in `pia_worker/settings.py` and `infrastructure/env.example`; the guard
test `test_threshold_corpus.py` fails if corpus and thresholds ever drift apart.

## 4. Accepted error trade-off (recorded per ADR-009)

- **Recall-leaning by design.** A missed eligibility hit is the product's worst failure
  (a silently lost placement opportunity); a false review ping costs the single user a
  glance. Over-inclusive similarity therefore lands in AMBIGUOUS + review, never in
  auto-ELIGIBLE — the state machine has no AMBIGUOUS→ELIGIBLE edge (ADR-005).
- **Known over-inclusion:** shared-token names ("Nitish Kumari" 0.960) reach the fuzzy
  band. Accepted: single-user review queue, dismissal is one action (FR-PRO-004 deny).
- **Known under-inclusion risk:** a heavily mangled name (< 0.90) is NOT_FOUND. The
  compensating controls are the identifier column (preferred signal, immune to name
  mangling) and user corrections (NOT_FOUND → MATCHED → ELIGIBLE, `confirm`).
- **Identifier dominance neutralizes the dangerous subset case:** "Nitish Kumar Singh"
  scores 1.0 as a name, but any comparable identifier mismatch decides NOT_FOUND before
  fuzzy is consulted (unit-tested as `TestIdentifierDominance`).

## 5. Confidence anchors (decisive stages, not tuned here — recorded for audit)

| Method | Confidence | Review flag |
|---|---:|---|
| IDENTIFIER + corroborating name | 0.99 | no |
| EXACT (raw name vs canonical/alias) | 0.97 | only on disambiguator conflict / low extraction confidence |
| NORMALIZED | 0.93 | same softening |
| IDENTIFIER, name not corroborating | 0.85 | **yes** |
| FUZZY / MODEL_REVIEW bands | measured sim | **always** |

## 6. Re-running the evaluation

```bash
.venv/Scripts/python -c "
import sys; sys.path[:0] = ['apps/worker/src', 'tests/fixtures']
from eligibility_corpus import CORPUS, PROFILE_NAME
from pia_shared.textnorm import normalize_name
from pia_worker.eligibility.matcher import name_similarity
for it in CORPUS:
    print(f\"{it['row']:24} {name_similarity(normalize_name(it['row']), PROFILE_NAME):6.3f}  {it['label']}\")
"
.venv/Scripts/python -m pytest tests/unit/test_threshold_corpus.py -q
```

## 7. Owner sign-off (2026-09-06) — P6 behavior decisions

The owner (Nitish) reviewed the four open P6 behavior decisions and confirmed the
conservative defaults implemented in this milestone. These are now binding product
decisions, pinned by the unit tests cited:

| # | Decision | Value | Pinned by |
|---|---|---|---|
| 1 | Fuzzy-only name hit (list has no identifier), sim ≥ FUZZY_MATCH_THRESHOLD | **AMBIGUOUS + requires_user_review — never auto-ELIGIBLE** (TRD §6 governance, ADR-005) | `test_fuzzy_band_is_ambiguous_never_matched` |
| 2 | Identifier matches, printed name does not corroborate | **ELIGIBLE with requires_user_review=true, confidence 0.85** (reg is unique; review flag surfaces it) | `test_identifier_match_uncorroborated_name_needs_review` |
| 3 | Lists where the profile does not appear | **Persist one NOT_FOUND record per list** (state NOT_FOUND, never NOT_ELIGIBLE — FR-ELG-009) | `test_no_match_is_not_found_not_not_eligible` |
| 4 | Disambiguator comparison | Token containment ("CAP-MCA" ≡ "MCA"); conflict only on disjoint tokens | `test_qualifier_tokens_count_as_agreement` |

Next milestone confirmed by owner: **P7 — Company memory (F-017/F-018)**, per the
ADR-010 phase order.
