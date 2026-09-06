"""User correction flows (FR-PRO-004) — pure transition chains from
pia_shared.states.CORRECTION_CHAINS, the logic behind POST /feedback/match.

Safety invariant (ADR-005/NFR-006): the AMBIGUOUS chain may only reach ELIGIBLE
through USER_CONFIRMED — user action is structurally required."""

import pytest

from pia_shared.enums import EligibilityState
from pia_shared.states import (
    CORRECTION_CHAINS,
    InvalidTransitionError,
    assert_valid_transition,
    correction_chain,
)


def _walk(chain: list[EligibilityState], start: EligibilityState) -> None:
    """Every chain step must be a legal §10.2 transition."""
    current = start
    for target in chain:
        assert_valid_transition("eligibility", current, target)
        current = target


class TestConfirmDecision:
    def test_ambiguous_completes_via_user_confirmed(self) -> None:
        chain = correction_chain("confirm", EligibilityState.AMBIGUOUS)
        assert chain == [EligibilityState.USER_CONFIRMED, EligibilityState.ELIGIBLE]
        _walk(chain, EligibilityState.AMBIGUOUS)

    def test_confirm_never_jumps_ambiguous_straight_to_eligible(self) -> None:
        chain = correction_chain("confirm", EligibilityState.AMBIGUOUS)
        assert EligibilityState.ELIGIBLE not in chain[:1]  # USER_CONFIRMED first
        with pytest.raises(InvalidTransitionError):
            assert_valid_transition(
                "eligibility", EligibilityState.AMBIGUOUS, EligibilityState.ELIGIBLE
            )

    def test_already_user_confirmed_advances_to_eligible(self) -> None:
        chain = correction_chain("confirm", EligibilityState.USER_CONFIRMED)
        assert chain == [EligibilityState.ELIGIBLE]

    def test_confirm_on_not_found_walks_matched_then_eligible(self) -> None:
        chain = correction_chain("confirm", EligibilityState.NOT_FOUND)
        assert chain == [EligibilityState.MATCHED, EligibilityState.ELIGIBLE]
        _walk(chain, EligibilityState.NOT_FOUND)

    def test_confirm_on_matched_advances_to_eligible(self) -> None:
        assert correction_chain("confirm", EligibilityState.MATCHED) == [
            EligibilityState.ELIGIBLE
        ]

    def test_confirm_not_applicable_to_eligible(self) -> None:
        assert correction_chain("confirm", EligibilityState.ELIGIBLE) == []


class TestDenyDecision:
    def test_deny_sends_ambiguous_to_not_found(self) -> None:
        chain = correction_chain("deny", EligibilityState.AMBIGUOUS)
        assert chain == [EligibilityState.NOT_FOUND]
        _walk(chain, EligibilityState.AMBIGUOUS)

    def test_deny_only_applies_to_ambiguous(self) -> None:
        for state in EligibilityState:
            if state is not EligibilityState.AMBIGUOUS:
                assert correction_chain("deny", state) == []


class TestWrongMatchDecision:
    def test_eligible_goes_to_superseded(self) -> None:
        chain = correction_chain("wrong_match", EligibilityState.ELIGIBLE)
        assert chain == [EligibilityState.SUPERSEDED]
        _walk(chain, EligibilityState.ELIGIBLE)

    def test_wrong_match_only_applies_to_eligible(self) -> None:
        for state in EligibilityState:
            if state is not EligibilityState.ELIGIBLE:
                assert correction_chain("wrong_match", state) == []


class TestLegacyDecisions:
    def test_missed_match_kept_as_alias_of_confirm_on_not_found(self) -> None:
        assert correction_chain("missed_match", EligibilityState.NOT_FOUND) == [
            EligibilityState.MATCHED,
            EligibilityState.ELIGIBLE,
        ]

    def test_unknown_decision_has_no_chain(self) -> None:
        assert correction_chain("make_me_eligible", EligibilityState.AMBIGUOUS) == []
        assert "make_me_eligible" not in CORRECTION_CHAINS


class TestGlobalSafetyInvariant:
    def test_no_decision_chain_contains_an_illegal_step(self) -> None:
        for chains in CORRECTION_CHAINS.values():
            for start, chain in chains.items():
                _walk(chain, start)  # raises InvalidTransitionError on any bad edge

    def test_ambiguous_reaches_eligible_only_through_user_confirmed(self) -> None:
        for chains in CORRECTION_CHAINS.values():
            chain = chains.get(EligibilityState.AMBIGUOUS)
            if chain and EligibilityState.ELIGIBLE in chain:
                assert chain.index(EligibilityState.USER_CONFIRMED) < chain.index(
                    EligibilityState.ELIGIBLE
                )
