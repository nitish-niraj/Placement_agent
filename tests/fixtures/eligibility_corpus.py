"""P6 threshold-tuning corpus (ADR-009 / open question Q8).

Name pairs are evaluated against the profile's normalized canonical name
("nitish kumar") by the stage-4 similarity metric — the corpus tunes ONLY the
two fuzzy thresholds. Names whose normalized form already equals a profile
alias are decided at stage 1/2 and are deliberately excluded here (their
similarity says nothing about the fuzzy bands).

Labels are human judgments recorded BEFORE thresholds were chosen:
- "me":         plausibly the profile owner under a printing/typo variant —
                must land at or above FUZZY_MATCH_THRESHOLD.
- "could_be_me": too close for a deterministic system to dismiss (possible
                typo, fuller name, shared tokens) but not provably the owner —
                must land at or above AMBIGUOUS_MATCH_THRESHOLD; such rows end
                in AMBIGUOUS + review under both bands, never auto-ELIGIBLE.
- "not_me":     clearly a different person — must land below
                AMBIGUOUS_MATCH_THRESHOLD.

The REAL same-name case ("Nitish Kumar Singh", registration 12528230) is
labelled could_be_me for name-only similarity: without an identifier the
string alone cannot exclude him, so review is the honest outcome. With his
registration number present the IDENTIFIER stage decides NOT_FOUND before any
fuzzy consideration (identifier dominance, matcher.py) — that behavior is
covered by unit tests, not by thresholds.
"""

PROFILE_NAME = "nitish kumar"

CORPUS: list[dict[str, str]] = [
    # --- "me": typo/printing variants of the owner (not covered by aliases) ---
    {"row": "Nitish Kummar", "label": "me", "note": "transposed chars in surname"},
    {"row": "Nithish Kumar", "label": "me", "note": "doubled h spelling variant"},
    {"row": "Kumaar Nitish", "label": "me",
     "note": "token order + vowel stretch (vs alias nitish kumaar)"},
    {"row": "NITISHKUMAR.", "label": "me", "note": "no space + trailing dot"},
    {"row": "Kumar Nitish", "label": "me", "note": "reversed token order"},
    # --- "could_be_me": close enough that dismissal would risk a false negative ---
    {"row": "Nitesh Kumar", "label": "could_be_me",
     "note": "first-name vowel variant; could be a typo or another student"},
    {"row": "Nitish Kumar Singh", "label": "could_be_me",
     "note": "REAL case (reg 12528230): name-only similarity cannot exclude "
             "him — identifier decides"},
    {"row": "Nitish Kumari", "label": "could_be_me",
     "note": "shared tokens over-include; accepted review noise (single-user)"},
    # --- "not_me": different people, must stay below the ambiguous band ---
    {"row": "Rahul Sharma", "label": "not_me", "note": "unrelated name"},
    {"row": "Amit Verma", "label": "not_me", "note": "unrelated name"},
    {"row": "Priya Singh", "label": "not_me", "note": "unrelated name"},
    {"row": "Nisha Kumari", "label": "not_me",
     "note": "same surname pattern, different first name"},
    {"row": "Nitish Ranjan", "label": "not_me",
     "note": "shared first name, different surname"},
    {"row": "Kumar Nitin", "label": "not_me", "note": "shared tokens, different person"},
]
