"""F-015 identity matcher — TRD §6 ladder over candidate-list rows.

Pure functions: no database, no LLM, no settings import — everything needed
arrives as arguments, so the whole ladder is unit-testable in isolation.

Ladder (evaluated per row; the outcome each stage reports is governed by the
docs, not by stage order alone):

1. EXACT       raw name equality (whitespace/case-insensitive) vs canonical
               name or an alias — decisive MATCHED.
2. NORMALIZED  normalize_name() equality (FR-ELG-005) — decisive MATCHED
               ("the workhorse stage", TRD §6).
3. IDENTIFIER  digits-only compare of registration/roll/student id (FR-ELG-006).
               PREFERRED signal when the row carries a comparable id: a hit is
               a decisive MATCHED (the name corroborates), a miss a decisive
               NOT_FOUND. This identifier dominance is what keeps "Nitish Kumar
               Singh / 12528230" from ever matching profile "Nitish Kumar /
               12515641" even though token-set name similarity is ~1.0 — a raw
               name match must never override a hard identifier mismatch.
4. FUZZY       token-set + full-string similarity vs every profile name.
               similarity >= FUZZY_MATCH_THRESHOLD -> AMBIGUOUS with review:
               per TRD §6 governance a fuzzy-only hit never auto-matches
               (ADR-005/NFR-006). Disambiguators (branch/batch, when present on
               BOTH sides) must agree or the hit is rejected.
5. MODEL_REVIEW proposal for a below-fuzzy-but-not-ignorable similarity
               (>= AMBIGUOUS_MATCH_THRESHOLD): status AMBIGUOUS, review only,
               never ELIGIBLE. The LLM-assisted reading of such cells lands in
               a later milestone; the review queue is the deterministic fallback.
"""

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from pia_shared.enums import EligibilityState, MatchMethod, MatchStatus
from pia_shared.schemas import CandidateRow, EvidenceRef, MatchResult
from pia_shared.textnorm import digits_only, normalize_name

# Confidence anchors for decisive stages (docs/07_P6_Threshold_Evaluation.md §4).
EXACT_CONFIDENCE = 0.97
NORMALIZED_CONFIDENCE = 0.93
IDENTIFIER_CORROBORATED_CONFIDENCE = 0.99
IDENTIFIER_UNCORROBORATED_CONFIDENCE = 0.85

# Identifier compares shorter than this are not decisive (a 1-3 digit roll
# number collides too easily to assert identity).
MIN_IDENTIFIER_DIGITS = 4

_IDENTIFIER_FIELDS = ("registration_number", "roll_number", "student_id")
_BRANCH_KEY = re.compile(r"branch|course|stream", re.IGNORECASE)
_BATCH_KEY = re.compile(r"batch|session", re.IGNORECASE)


@dataclass(frozen=True)
class MatchThresholds:
    """Tuned on the P6 fixture corpus (ADR-009 — never invented values).

    See docs/07_P6_Threshold_Evaluation.md for the evaluation table, the
    chosen values, and the accepted error trade-off."""

    fuzzy: float
    ambiguous: float
    confidence_floor: float = 0.0  # extraction-confidence floor (P5 vision path)


@dataclass(frozen=True)
class IdentityProfile:
    """The one candidate profile, pre-normalized for ladder lookups."""

    profile_id: str
    canonical_name: str
    normalized_names: frozenset[str]  # normalized canonical name + aliases
    registration_number: str | None = None
    roll_number: str | None = None
    student_id: str | None = None
    branch: str | None = None
    batch: str | None = None
    aliases: tuple[str, ...] = field(default=())

    @property
    def identifiers(self) -> dict[str, str]:
        """digits-only identifiers by field name (comparable values only)."""
        values = {}
        for field_name in _IDENTIFIER_FIELDS:
            raw = getattr(self, field_name)
            digits = digits_only(raw)
            if digits:
                values[field_name] = digits
        return values


def name_similarity(a: str, b: str) -> float:
    """Token-set + full-string similarity on already-normalized names (TRD §6
    stage 4: "token-set ratio + edit distance"; SequenceMatcher is an edit-style
    ratio). Token-set handles token order ("kumar nitish") and subset names
    ("nitish kumar singh"); the full-string ratio catches character-level typos."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    full = SequenceMatcher(None, a, b).ratio()
    tokens_a, tokens_b = set(a.split()), set(b.split())
    if not tokens_a or not tokens_b:
        return full
    inter_set = tokens_a & tokens_b
    intersection = " ".join(sorted(inter_set))
    t0 = (intersection + " " + " ".join(sorted(tokens_a - inter_set))).strip()
    t1 = (intersection + " " + " ".join(sorted(tokens_b - inter_set))).strip()
    candidates = [SequenceMatcher(None, t0, t1).ratio()]
    if intersection:
        candidates.append(SequenceMatcher(None, intersection, t0).ratio())
        candidates.append(SequenceMatcher(None, intersection, t1).ratio())
    return max(full, *candidates)


def best_name_similarity(name_norm: str, profile: IdentityProfile) -> float:
    return max(
        (name_similarity(name_norm, n) for n in profile.normalized_names),
        default=0.0,
    )


def _row_identifiers(row: CandidateRow) -> dict[str, str]:
    values = {}
    for field_name in _IDENTIFIER_FIELDS:
        digits = digits_only(getattr(row, field_name))
        if digits:
            values[field_name] = digits
    return values


def _other_identifier(row: CandidateRow, pattern: re.Pattern[str]) -> str | None:
    for key, value in row.other_identifiers.items():
        if pattern.search(key) and value:
            return normalize_name(value)
    return None


def _disambiguator_conflicts(row: CandidateRow, profile: IdentityProfile) -> list[str]:
    """branch/batch values printed on BOTH sides that disagree (TRD §6 stage 4).

    Token containment counts as agreement: real lists print qualifiers the
    profile omits ('CAP-MCA' vs 'MCA'), which is the same program — a conflict
    requires genuinely disjoint tokens ('B.Tech CSE' vs 'MCA')."""
    conflicts = []
    for pattern, profile_value, label in (
        (_BRANCH_KEY, profile.branch, "branch"),
        (_BATCH_KEY, profile.batch, "batch"),
    ):
        row_value = _other_identifier(row, pattern)
        if row_value and profile_value:
            row_tokens = set(row_value.split())
            profile_tokens = set(normalize_name(profile_value).split())
            if not (row_tokens <= profile_tokens or profile_tokens <= row_tokens):
                conflicts.append(f"{label} {row_value!r} != profile "
                                 f"{normalize_name(profile_value)!r}")
    return conflicts


def _row_evidence(row: CandidateRow) -> EvidenceRef:
    location = f"{row.source_page_or_sheet}!row {row.row_index}" if row.source_page_or_sheet \
        else f"row {row.row_index}"
    identifier = row.registration_number or row.roll_number or row.student_id
    quote = f"{row.raw_name}" + (f" ({identifier})" if identifier else "")
    return EvidenceRef(kind="row", location=location, quote=quote)


def _low_extraction_confidence(row: CandidateRow, thresholds: MatchThresholds) -> bool:
    return (
        row.extraction_confidence > 0
        and row.extraction_confidence < thresholds.confidence_floor
    )


def evaluate_row(
    row: CandidateRow, profile: IdentityProfile, thresholds: MatchThresholds
) -> MatchResult:
    """Run the ladder for one candidate row. First decisive stage wins."""
    name_norm = row.normalized_name or normalize_name(row.raw_name)
    evidence = [_row_evidence(row)]
    row_ids = _row_identifiers(row)
    profile_ids = profile.identifiers

    # --- Stage 3 IDENTIFIER (preferred signal — dominates name stages when the
    # row carries a comparable id; see module docstring for why it runs first).
    comparable = [
        (field_name, row_ids[field_name], profile_ids[field_name])
        for field_name in row_ids
        if field_name in profile_ids
        and len(row_ids[field_name]) >= MIN_IDENTIFIER_DIGITS
        and len(profile_ids[field_name]) >= MIN_IDENTIFIER_DIGITS
    ]
    if comparable:
        if any(row_digits == profile_digits for _, row_digits, profile_digits in comparable):
            corroborated = name_norm in profile.normalized_names or (
                best_name_similarity(name_norm, profile) >= thresholds.ambiguous
            )
            conflicts = _disambiguator_conflicts(row, profile)
            if corroborated and not conflicts:
                return MatchResult(
                    status=MatchStatus.MATCHED,
                    match_method=MatchMethod.IDENTIFIER,
                    confidence=IDENTIFIER_CORROBORATED_CONFIDENCE,
                    evidence=evidence,
                    matched_identity_id=profile.profile_id,
                    matched_row_index=row.row_index,
                    rationale=f"identifier {comparable[0][1]} matches profile; "
                              "name corroborates",
                )
            return MatchResult(
                status=MatchStatus.MATCHED,
                match_method=MatchMethod.IDENTIFIER,
                confidence=IDENTIFIER_UNCORROBORATED_CONFIDENCE,
                evidence=evidence,
                requires_user_review=True,
                matched_identity_id=profile.profile_id,
                matched_row_index=row.row_index,
                rationale="identifier matches but name does not corroborate"
                          + (f"; {'; '.join(conflicts)}" if conflicts else ""),
            )
        # Hard identifier mismatch: the row belongs to a different student.
        # Name similarity is irrelevant here (identifier dominance).
        return MatchResult(
            status=MatchStatus.NOT_FOUND,
            match_method=MatchMethod.IDENTIFIER,
            confidence=round(1 - best_name_similarity(name_norm, profile), 3),
            evidence=evidence,
            matched_row_index=row.row_index,
            rationale="identifier present and does not match profile (name "
                      "similarity cannot override a hard id mismatch)",
        )

    # --- Stages 1-2: name equality.
    raw_key = " ".join((row.raw_name or "").split()).casefold()
    if raw_key and raw_key in {n.casefold() for n in
                               (profile.canonical_name, *profile.aliases) if n.strip()}:
        return _decisive_name_match(
            row, profile, MatchMethod.EXACT, EXACT_CONFIDENCE, evidence, thresholds
        )
    if name_norm and name_norm in profile.normalized_names:
        return _decisive_name_match(
            row, profile, MatchMethod.NORMALIZED, NORMALIZED_CONFIDENCE, evidence, thresholds
        )

    # --- Stages 4-5: similarity bands. A fuzzy hit is review evidence only.
    similarity = round(best_name_similarity(name_norm, profile), 3)
    conflicts = _disambiguator_conflicts(row, profile)
    if conflicts:  # disambiguators must agree for any fuzzy consideration
        return MatchResult(
            status=MatchStatus.NOT_FOUND,
            match_method=MatchMethod.FUZZY,
            confidence=similarity,
            evidence=evidence,
            matched_row_index=row.row_index,
            rationale=f"disambiguator conflict: {'; '.join(conflicts)}",
        )
    if similarity >= thresholds.fuzzy:
        return MatchResult(
            status=MatchStatus.AMBIGUOUS,
            match_method=MatchMethod.FUZZY,
            confidence=similarity,
            evidence=evidence,
            requires_user_review=True,
            matched_row_index=row.row_index,
            rationale=f"fuzzy-only hit (similarity {similarity} >= {thresholds.fuzzy}): "
                      "name evidence without an identifier never auto-matches (ADR-005)",
        )
    if similarity >= thresholds.ambiguous:
        return MatchResult(
            status=MatchStatus.AMBIGUOUS,
            match_method=MatchMethod.MODEL_REVIEW,
            confidence=similarity,
            evidence=evidence,
            requires_user_review=True,
            matched_row_index=row.row_index,
            rationale=f"similarity {similarity} in review band "
                      f"[{thresholds.ambiguous}, {thresholds.fuzzy}): queued for "
                      "model/human reading (proposal only, never ELIGIBLE)",
        )
    return MatchResult(
        status=MatchStatus.NOT_FOUND,
        match_method=MatchMethod.FUZZY,
        confidence=similarity,
        evidence=evidence,
        matched_row_index=row.row_index,
        rationale=f"no ladder stage decisive (best similarity {similarity} "
                  f"< {thresholds.ambiguous})",
    )


def _decisive_name_match(
    row: CandidateRow, profile: IdentityProfile, method: MatchMethod,
    confidence: float, evidence: list[EvidenceRef], thresholds: MatchThresholds,
) -> MatchResult:
    """EXACT/NORMALIZED hit — decisive per TRD §6, softened by context checks."""
    conflicts = _disambiguator_conflicts(row, profile)
    review = bool(conflicts) or _low_extraction_confidence(row, thresholds)
    rationale = f"{method.value} name match against profile"
    if conflicts:
        rationale += f" (review: {'; '.join(conflicts)})"
    if _low_extraction_confidence(row, thresholds):
        rationale += " (review: extraction confidence below floor)"
    return MatchResult(
        status=MatchStatus.MATCHED,
        match_method=method,
        confidence=confidence,
        evidence=evidence,
        requires_user_review=review,
        matched_identity_id=profile.profile_id,
        matched_row_index=row.row_index,
        rationale=rationale,
    )


@dataclass(frozen=True)
class ListOutcome:
    """Aggregated result for ONE candidate list — the input to the eligibility
    state machine (F-016). Exactly one eligibility_records row is written per
    list from this outcome."""

    status: MatchStatus
    state: EligibilityState  # target eligibility_state for the record
    match_method: MatchMethod | None
    confidence: float
    requires_user_review: bool
    evidence: list[EvidenceRef]
    rationale: str
    row_results: list[MatchResult] = field(default_factory=list)


def evaluate_list(
    rows: list[CandidateRow], profile: IdentityProfile, thresholds: MatchThresholds
) -> ListOutcome:
    """Aggregate row results into one list outcome (§10.2 semantics).

    - any identifier-matched row        -> MATCHED (the id proves I am listed)
    - exactly one name-decisive row and no same-name collision -> MATCHED
    - >=2 name-equal rows / collisions  -> AMBIGUOUS (same-name rule)
    - fuzzy/review hits only            -> AMBIGUOUS (never auto-match)
    - nothing                           -> NOT_FOUND (FR-ELG-009: NOT_FOUND is
                                           NOT NOT_ELIGIBLE)
    """
    if not rows:
        raise ValueError("evaluate_list requires at least one row")

    # Position-paired by index: row_index provenance can repeat across sheets,
    # and pydantic equality between results is not row identity.
    results = [evaluate_row(row, profile, thresholds) for row in rows]
    identifier_hits = [r for r in results
                       if r.status is MatchStatus.MATCHED
                       and r.match_method is MatchMethod.IDENTIFIER]
    name_hit_idx = [
        i for i, r in enumerate(results)
        if r.status is MatchStatus.MATCHED
        and r.match_method in (MatchMethod.EXACT, MatchMethod.NORMALIZED)
    ]
    ambiguous = [r for r in results if r.status is MatchStatus.AMBIGUOUS]

    if identifier_hits:
        return _outcome_from(
            MatchStatus.MATCHED, EligibilityState.MATCHED, identifier_hits, results,
            rationale="identifier match proves the profile is on this list",
        )

    if name_hit_idx:
        matched_name = normalize_name(rows[name_hit_idx[0]].raw_name)
        collision_idx = [
            i for i, r in enumerate(results)
            if i not in name_hit_idx
            and normalize_name(rows[i].raw_name) == matched_name
        ]
        if len(name_hit_idx) > 1 or collision_idx:
            return _outcome_from(
                MatchStatus.AMBIGUOUS, EligibilityState.AMBIGUOUS,
                [results[i] for i in name_hit_idx + collision_idx], results,
                rationale="same-name rows without distinguishing identifiers "
                          "cannot be resolved deterministically",
                review=True,
            )
        return _outcome_from(
            MatchStatus.MATCHED, EligibilityState.MATCHED, [results[name_hit_idx[0]]],
            results, rationale="single name-decisive row",
        )

    if ambiguous:
        method = (MatchMethod.FUZZY if any(r.match_method is MatchMethod.FUZZY
                                           for r in ambiguous) else MatchMethod.MODEL_REVIEW)
        return _outcome_from(
            MatchStatus.AMBIGUOUS, EligibilityState.AMBIGUOUS, ambiguous, results,
            rationale="fuzzy-only candidates require user review (ADR-005)",
            method=method, review=True,
        )

    return ListOutcome(
        status=MatchStatus.NOT_FOUND,
        state=EligibilityState.NOT_FOUND,
        match_method=MatchMethod.FUZZY,
        confidence=max(r.confidence for r in results),
        requires_user_review=False,
        evidence=[],  # no match — nothing to cite as proof of me
        rationale="no row matched any ladder stage (NOT_FOUND is not NOT_ELIGIBLE, "
                  "FR-ELG-009)",
        row_results=results,
    )


def _outcome_from(
    status: MatchStatus, state: EligibilityState, cited: list[MatchResult],
    row_results: list[MatchResult], rationale: str,
    method: MatchMethod | None = None, review: bool = False,
) -> ListOutcome:
    best = max(cited, key=lambda r: r.confidence)
    return ListOutcome(
        status=status,
        state=state,
        match_method=method or best.match_method,
        confidence=best.confidence,
        requires_user_review=review or any(r.requires_user_review for r in cited),
        evidence=[e for r in cited for e in r.evidence],
        rationale=rationale,
        row_results=row_results,  # every evaluated row — the audit trail
    )
