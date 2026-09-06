"""State machine invariants — the safety core (ADR-005, NFR-006, master §10)."""

import pytest

from pia_shared.enums import (
    ActionStatus,
    DeadlineState,
    EligibilityState,
    EventStatus,
    MessageState,
)
from pia_shared.states import (
    InvalidTransitionError,
    assert_valid_transition,
    can_transition,
)


class TestMessageStateMachine:
    def test_happy_path(self) -> None:
        for cur, nxt in [
            (MessageState.RECEIVED, MessageState.VALIDATED),
            (MessageState.VALIDATED, MessageState.QUEUED),
            (MessageState.QUEUED, MessageState.PROCESSING),
            (MessageState.PROCESSING, MessageState.PROCESSED),
        ]:
            assert_valid_transition("message", cur, nxt)

    def test_retry_loop(self) -> None:
        assert_valid_transition("message", MessageState.PROCESSING, MessageState.RETRYING)
        assert_valid_transition("message", MessageState.RETRYING, MessageState.PROCESSING)

    def test_failed_is_terminal(self) -> None:
        assert_valid_transition("message", MessageState.PROCESSING, MessageState.FAILED)
        assert not can_transition("message", MessageState.FAILED, MessageState.PROCESSING)
        assert not can_transition("message", MessageState.FAILED, MessageState.PROCESSED)

    def test_processed_is_terminal(self) -> None:
        assert not can_transition("message", MessageState.PROCESSED, MessageState.QUEUED)


class TestEligibilityStateMachine:
    def test_ambiguous_never_auto_confirms(self) -> None:
        """ADR-005 / NFR-006: no AMBIGUOUS -> ELIGIBLE edge may exist, ever."""
        assert not can_transition(
            "eligibility", EligibilityState.AMBIGUOUS, EligibilityState.ELIGIBLE
        )
        assert_valid_transition(
            "eligibility", EligibilityState.AMBIGUOUS, EligibilityState.USER_CONFIRMED
        )
        assert_valid_transition(
            "eligibility", EligibilityState.USER_CONFIRMED, EligibilityState.ELIGIBLE
        )

    def test_not_found_is_distinct_from_not_eligible(self) -> None:
        """FR-ELG-009: absence from one list must never claim ineligibility."""
        assert_valid_transition(
            "eligibility", EligibilityState.UNKNOWN, EligibilityState.NOT_FOUND
        )
        assert_valid_transition(
            "eligibility", EligibilityState.UNKNOWN, EligibilityState.NOT_ELIGIBLE
        )
        assert not can_transition(
            "eligibility", EligibilityState.NOT_FOUND, EligibilityState.NOT_ELIGIBLE
        )

    def test_corrections_reopen_states(self) -> None:
        assert_valid_transition(
            "eligibility", EligibilityState.NOT_FOUND, EligibilityState.MATCHED
        )
        assert_valid_transition(
            "eligibility", EligibilityState.ELIGIBLE, EligibilityState.SUPERSEDED
        )

    def test_matched_requires_no_review(self) -> None:
        assert_valid_transition(
            "eligibility", EligibilityState.UNKNOWN, EligibilityState.MATCHED
        )
        assert_valid_transition(
            "eligibility", EligibilityState.MATCHED, EligibilityState.ELIGIBLE
        )


class TestEventAndDeadlineMachines:
    def test_event_delta_keeps_active(self) -> None:
        assert_valid_transition("event", EventStatus.ACTIVE, EventStatus.ACTIVE)

    def test_event_terminal_states(self) -> None:
        assert_valid_transition("event", EventStatus.DETECTED, EventStatus.ACTIVE)
        assert_valid_transition("event", EventStatus.ACTIVE, EventStatus.EXPIRED)
        assert not can_transition("event", EventStatus.EXPIRED, EventStatus.ACTIVE)

    def test_deadline_flow(self) -> None:
        assert_valid_transition("deadline", DeadlineState.OPEN, DeadlineState.DUE_SOON)
        assert_valid_transition("deadline", DeadlineState.DUE_SOON, DeadlineState.EXPIRED)
        assert_valid_transition("deadline", DeadlineState.OPEN, DeadlineState.CANCELLED)
        assert not can_transition("deadline", DeadlineState.COMPLETED, DeadlineState.OPEN)


class TestActionMachine:
    def test_no_execution_without_approval(self) -> None:
        """SEC-004/005: EXECUTING is unreachable without APPROVED."""
        assert not can_transition("action", ActionStatus.PROPOSED, ActionStatus.EXECUTING)
        assert not can_transition("action", ActionStatus.WAITING_APPROVAL, ActionStatus.EXECUTING)
        assert_valid_transition("action", ActionStatus.PROPOSED, ActionStatus.WAITING_APPROVAL)
        assert_valid_transition("action", ActionStatus.WAITING_APPROVAL, ActionStatus.APPROVED)
        assert_valid_transition("action", ActionStatus.APPROVED, ActionStatus.EXECUTING)


class TestMachineGuard:
    def test_unknown_machine_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown state machine"):
            assert_valid_transition("nope", "a", "b")  # type: ignore[arg-type]

    def test_illegal_transition_message(self) -> None:
        with pytest.raises(InvalidTransitionError, match="illegal transition"):
            assert_valid_transition("message", MessageState.RECEIVED, MessageState.PROCESSED)
