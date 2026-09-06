"""F-015 identity matcher — ladder stages, identifier dominance, ambiguity
safety (P6 acceptance: the Singh case must be NOT_FOUND; same-name ambiguity
must never auto-confirm — ADR-005/NFR-006)."""

import pytest

from pia_shared.enums import EligibilityState, MatchMethod, MatchStatus
from pia_shared.schemas import CandidateRow
from pia_shared.textnorm import normalize_name
from pia_worker.eligibility.matcher import (
    IdentityProfile,
    MatchThresholds,
    evaluate_list,
    evaluate_row,
    name_similarity,
)

# Mirrors the LIVE identity_aliases rows of the real profile (11 aliases).
ALIASES = (
    "nitish kumar", "nitishkumar", "nitish kumr", "nitishkumr", "nitiskumar",
    "nitish", "nitish k", "nitish kumaar", "kumar nitish",
    "nitish kumar lpu", "nitish kumar mca",
)
PROFILE = IdentityProfile(
    profile_id="00000000-0000-0000-0000-000000000002",
    canonical_name="Nitish Kumar",
    normalized_names=frozenset(
        ["nitish kumar", *[normalize_name(a) for a in ALIASES]]
    ),
    registration_number="12515641",  # THE key identifier (decrypted by records.py)
    branch="MCA",
    batch="2025-2027",
    aliases=ALIASES,
)
THRESHOLDS = MatchThresholds(fuzzy=0.95, ambiguous=0.90, confidence_floor=0.75)


def row(
    name: str, reg: str | None = None, roll: str | None = None,
    others: dict[str, str] | None = None, conf: float = 0.95, idx: int = 0,
) -> CandidateRow:
    return CandidateRow(
        raw_name=name,
        normalized_name=normalize_name(name),
        registration_number=reg,
        roll_number=roll,
        other_identifiers=others or {},
        extraction_confidence=conf,
        source_page_or_sheet="Sheet1",
        row_index=idx,
    )


class TestLadderStages:
    def test_exact_stage_decisive(self) -> None:
        result = evaluate_row(row("Nitish Kumar", idx=1), PROFILE, THRESHOLDS)
        assert result.status is MatchStatus.MATCHED
        assert result.match_method is MatchMethod.EXACT
        assert result.requires_user_review is False
        assert result.evidence[0].location == "Sheet1!row 1"

    def test_normalized_stage_is_the_workhorse(self) -> None:
        for name in ("NITISH KUMAR.", "nitish-kumar", "Mr. Nitish Kumar"):
            result = evaluate_row(row(name), PROFILE, THRESHOLDS)
            assert result.status is MatchStatus.MATCHED, name
            assert result.match_method is MatchMethod.NORMALIZED, name

    def test_alias_hit_is_exact(self) -> None:
        # EXACT compares against canonical name AND aliases (TRD §6 stage 1);
        # stored aliases are lowercase, so the raw string equals one directly.
        result = evaluate_row(row("Nitish Kumr"), PROFILE, THRESHOLDS)
        assert result.status is MatchStatus.MATCHED
        assert result.match_method is MatchMethod.EXACT

    def test_alias_via_normalization_stage2(self) -> None:
        result = evaluate_row(row("Nitish Kumr."), PROFILE, THRESHOLDS)
        assert result.status is MatchStatus.MATCHED
        assert result.match_method is MatchMethod.NORMALIZED

    def test_identifier_match_with_corroborating_name(self) -> None:
        result = evaluate_row(row("Nitish Kumar", reg="12515641"), PROFILE, THRESHOLDS)
        assert result.status is MatchStatus.MATCHED
        assert result.match_method is MatchMethod.IDENTIFIER
        assert result.confidence == pytest.approx(0.99)
        assert result.requires_user_review is False

    def test_identifier_match_survives_name_formatting(self) -> None:
        result = evaluate_row(row("NITISH KUMAR", reg="REG-12515641"), PROFILE, THRESHOLDS)
        assert result.status is MatchStatus.MATCHED
        assert result.match_method is MatchMethod.IDENTIFIER

    def test_identifier_match_uncorroborated_name_needs_review(self) -> None:
        result = evaluate_row(row("R. Kumar", reg="12515641"), PROFILE, THRESHOLDS)
        assert result.status is MatchStatus.MATCHED
        assert result.match_method is MatchMethod.IDENTIFIER
        assert result.requires_user_review is True
        assert result.confidence == pytest.approx(0.85)


class TestIdentifierDominance:
    """TRD §6: the identifier is the preferred signal — a hard id mismatch
    decides BEFORE name similarity, in both directions."""

    def test_singh_case_is_not_found(self) -> None:
        result = evaluate_row(row("Nitish Kumar Singh", reg="12528230"), PROFILE, THRESHOLDS)
        assert result.status is MatchStatus.NOT_FOUND
        assert result.match_method is MatchMethod.IDENTIFIER
        assert result.requires_user_review is False

    def test_exact_name_cannot_override_identifier_mismatch(self) -> None:
        result = evaluate_row(row("Nitish Kumar", reg="12528230"), PROFILE, THRESHOLDS)
        assert result.status is MatchStatus.NOT_FOUND
        assert result.match_method is MatchMethod.IDENTIFIER

    def test_fuzzy_close_name_cannot_override_identifier_mismatch(self) -> None:
        result = evaluate_row(row("Nitish Kummar", reg="12528230"), PROFILE, THRESHOLDS)
        assert result.status is MatchStatus.NOT_FOUND

    def test_non_comparable_identifier_falls_through_to_names(self) -> None:
        # roll 999 vs profile without a roll: nothing comparable → name stages
        result = evaluate_row(row("Nitish Kumar", roll="999"), PROFILE, THRESHOLDS)
        assert result.status is MatchStatus.MATCHED
        assert result.match_method is MatchMethod.EXACT


class TestFuzzyAndReviewBands:
    def test_fuzzy_band_is_ambiguous_never_matched(self) -> None:
        result = evaluate_row(row("Nitish Kummar"), PROFILE, THRESHOLDS)
        assert result.status is MatchStatus.AMBIGUOUS
        assert result.match_method is MatchMethod.FUZZY
        assert result.requires_user_review is True
        assert result.confidence >= THRESHOLDS.fuzzy

    def test_review_band_is_model_review_proposal(self) -> None:
        result = evaluate_row(row("Nitesh Kumar"), PROFILE, THRESHOLDS)
        assert result.status is MatchStatus.AMBIGUOUS
        assert result.match_method is MatchMethod.MODEL_REVIEW
        assert result.requires_user_review is True
        assert THRESHOLDS.ambiguous <= result.confidence < THRESHOLDS.fuzzy

    def test_below_ambiguous_band_is_not_found(self) -> None:
        result = evaluate_row(row("Rahul Sharma"), PROFILE, THRESHOLDS)
        assert result.status is MatchStatus.NOT_FOUND
        assert result.confidence < THRESHOLDS.ambiguous

    def test_disambiguator_conflict_rejects_fuzzy(self) -> None:
        result = evaluate_row(
            row("Nitish Kummar", others={"Branch": "CSE"}), PROFILE, THRESHOLDS
        )
        assert result.status is MatchStatus.NOT_FOUND
        assert "branch" in result.rationale

    def test_disambiguator_conflict_softens_decisive_match(self) -> None:
        result = evaluate_row(
            row("Nitish Kumar", others={"Branch": "CSE"}), PROFILE, THRESHOLDS
        )
        assert result.status is MatchStatus.MATCHED
        assert result.requires_user_review is True

    def test_qualifier_tokens_count_as_agreement(self) -> None:
        # Real LPU lists print "CAP-MCA" where the profile says "MCA" — the
        # same program, not a conflict (token containment).
        result = evaluate_row(
            row("Nitish Kumar", reg="12515641", others={"Stream": "CAP-MCA"}),
            PROFILE, THRESHOLDS,
        )
        assert result.status is MatchStatus.MATCHED
        assert result.match_method is MatchMethod.IDENTIFIER
        assert result.confidence == pytest.approx(0.99)
        assert result.requires_user_review is False

    def test_genuinely_disjoint_branch_tokens_conflict(self) -> None:
        result = evaluate_row(
            row("Nitish Kumar", reg="12515641", others={"Stream": "B.Tech CSE"}),
            PROFILE, THRESHOLDS,
        )
        assert result.status is MatchStatus.MATCHED
        assert result.match_method is MatchMethod.IDENTIFIER
        assert result.requires_user_review is True
        assert "branch" in result.rationale

    def test_low_extraction_confidence_flags_decisive_match(self) -> None:
        result = evaluate_row(row("Nitish Kumar", conf=0.4), PROFILE, THRESHOLDS)
        assert result.status is MatchStatus.MATCHED
        assert result.requires_user_review is True
        assert "extraction confidence" in result.rationale


class TestListAggregation:
    def test_identifier_match_aggregates_to_matched(self) -> None:
        rows = [
            row("Rahul Sharma", reg="12528001", idx=1),
            row("Nitish Kumar Singh", reg="12528230", idx=2),  # must NOT be cited
            row("Nitish Kumar", reg="12515641", idx=3),
        ]
        outcome = evaluate_list(rows, PROFILE, THRESHOLDS)
        assert outcome.status is MatchStatus.MATCHED
        assert outcome.state is EligibilityState.MATCHED
        assert outcome.match_method is MatchMethod.IDENTIFIER
        assert [e.location for e in outcome.evidence] == ["Sheet1!row 3"]
        # the Singh row is evaluated and individually NOT_FOUND
        singh = next(r for r in outcome.row_results
                     if r.matched_row_index == 2)
        assert singh.status is MatchStatus.NOT_FOUND

    def test_same_name_rows_without_ids_are_ambiguous(self) -> None:
        rows = [row("Nitish Kumar", idx=1), row("NITISH KUMAR", idx=2)]
        outcome = evaluate_list(rows, PROFILE, THRESHOLDS)
        assert outcome.status is MatchStatus.AMBIGUOUS
        assert outcome.state is EligibilityState.AMBIGUOUS
        assert outcome.requires_user_review is True
        assert len(outcome.evidence) == 2  # both rows cited for review

    def test_name_match_colliding_with_same_name_foreign_id_is_ambiguous(self) -> None:
        rows = [
            row("Nitish Kumar", idx=1),                            # no id
            row("Nitish Kumar", reg="12528230", idx=2),            # someone else
        ]
        outcome = evaluate_list(rows, PROFILE, THRESHOLDS)
        assert outcome.status is MatchStatus.AMBIGUOUS
        assert outcome.requires_user_review is True

    def test_fuzzy_only_list_is_ambiguous(self) -> None:
        outcome = evaluate_list([row("Nitish Kummar", idx=1)], PROFILE, THRESHOLDS)
        assert outcome.status is MatchStatus.AMBIGUOUS
        assert outcome.state is EligibilityState.AMBIGUOUS
        assert outcome.match_method is MatchMethod.FUZZY

    def test_no_match_is_not_found_not_not_eligible(self) -> None:
        rows = [row("Rahul Sharma", idx=1), row("Amit Verma", idx=2)]
        outcome = evaluate_list(rows, PROFILE, THRESHOLDS)
        assert outcome.status is MatchStatus.NOT_FOUND
        assert outcome.state is EligibilityState.NOT_FOUND  # FR-ELG-009
        assert outcome.state is not EligibilityState.NOT_ELIGIBLE
        assert outcome.evidence == []

    def test_empty_list_rejected(self) -> None:
        with pytest.raises(ValueError):
            evaluate_list([], PROFILE, THRESHOLDS)


class TestSimilarityMetric:
    def test_identical_and_reordered_tokens(self) -> None:
        assert name_similarity("nitish kumar", "nitish kumar") == 1.0
        assert name_similarity("kumar nitish", "nitish kumar") == 1.0

    def test_empty_inputs(self) -> None:
        assert name_similarity("", "nitish kumar") == 0.0

    def test_singh_similarity_is_high_hence_identifier_decides(self) -> None:
        # The reason identifier dominance is mandatory: the name alone is
        # indistinguishable from a fuller variant of the profile name.
        assert name_similarity("nitish kumar singh", "nitish kumar") >= THRESHOLDS.fuzzy
