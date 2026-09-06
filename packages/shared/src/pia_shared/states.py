"""State machines — docs/05_Backend_Schema.md §2 (master doc §10.1–10.4, FR-EVT-003).

Transitions are enforced in the state-transition layer and validated by unit tests,
not by DB triggers. Two invariants carry safety weight:
- No AMBIGUOUS -> ELIGIBLE edge exists (ADR-005, NFR-006): an ambiguous identity
  match can never become ELIGIBLE without explicit user confirmation.
- FAILED is terminal for message processing and always requires a visible reason
  (master §10.1: "terminal FAILED requires visible reason").
"""

from pia_shared.enums import (
    ActionStatus,
    CompanyLifecycle,
    DeadlineState,
    EligibilityState,
    EventStatus,
    MessageState,
)

State = (
    MessageState | EligibilityState | EventStatus | DeadlineState | ActionStatus
    | CompanyLifecycle
)


class InvalidTransitionError(ValueError):
    """Raised when a state transition is not allowed by its state machine."""


MESSAGE_TRANSITIONS: dict[MessageState, set[MessageState]] = {
    MessageState.RECEIVED: {MessageState.VALIDATED},
    MessageState.VALIDATED: {MessageState.QUEUED},
    MessageState.QUEUED: {MessageState.PROCESSING},
    MessageState.PROCESSING: {
        MessageState.PROCESSED,
        MessageState.RETRYING,
        MessageState.FAILED,
    },
    MessageState.RETRYING: {MessageState.PROCESSING, MessageState.FAILED},
    MessageState.PROCESSED: set(),
    MessageState.FAILED: set(),  # terminal — requires visible failure_reason
}

ELIGIBILITY_TRANSITIONS: dict[EligibilityState, set[EligibilityState]] = {
    EligibilityState.UNKNOWN: {
        EligibilityState.MATCHED,
        EligibilityState.NOT_FOUND,
        EligibilityState.AMBIGUOUS,
        EligibilityState.NOT_ELIGIBLE,  # only from explicit source evidence (FR-ELG-009)
    },
    EligibilityState.MATCHED: {EligibilityState.ELIGIBLE},
    EligibilityState.AMBIGUOUS: {
        EligibilityState.USER_CONFIRMED,
        EligibilityState.NOT_FOUND,  # user denies it is them
    },
    EligibilityState.USER_CONFIRMED: {EligibilityState.ELIGIBLE},
    EligibilityState.ELIGIBLE: {
        EligibilityState.SUPERSEDED,
        EligibilityState.EXPIRED,
    },
    EligibilityState.NOT_FOUND: {EligibilityState.MATCHED},  # later correction (FR-PRO-004)
    EligibilityState.NOT_ELIGIBLE: {EligibilityState.MATCHED},  # correction with new evidence
    EligibilityState.EXPIRED: {EligibilityState.MATCHED},  # re-eligible on a fresh list
    EligibilityState.SUPERSEDED: set(),
    # NOTE: there is deliberately no AMBIGUOUS -> ELIGIBLE edge (ADR-005).
}

# User correction flows (FR-PRO-004, POST /feedback/match): decision -> the
# transition CHAIN each current state walks through, one assert_valid_transition
# step at a time. "confirm" on an ambiguous record intentionally completes the
# documented AMBIGUOUS -> USER_CONFIRMED -> ELIGIBLE path — that user action IS
# the only way an ambiguous match becomes ELIGIBLE (ADR-005/NFR-006).
CORRECTION_CHAINS: dict[str, dict[EligibilityState, list[EligibilityState]]] = {
    "confirm": {
        EligibilityState.AMBIGUOUS: [EligibilityState.USER_CONFIRMED, EligibilityState.ELIGIBLE],
        EligibilityState.USER_CONFIRMED: [EligibilityState.ELIGIBLE],
        EligibilityState.NOT_FOUND: [EligibilityState.MATCHED, EligibilityState.ELIGIBLE],
        EligibilityState.MATCHED: [EligibilityState.ELIGIBLE],
    },
    "deny": {
        EligibilityState.AMBIGUOUS: [EligibilityState.NOT_FOUND],
    },
    "wrong_match": {
        EligibilityState.ELIGIBLE: [EligibilityState.SUPERSEDED],
    },
    # Legacy decision name kept for API compatibility (P4 clients).
    "missed_match": {
        EligibilityState.NOT_FOUND: [EligibilityState.MATCHED, EligibilityState.ELIGIBLE],
    },
}


def correction_chain(decision: str, current: EligibilityState) -> list[EligibilityState]:
    """Transition chain for a user correction, or [] when the decision does not
    apply to the current state (caller surfaces that as a user-facing error)."""
    return CORRECTION_CHAINS.get(decision, {}).get(current, [])

EVENT_TRANSITIONS: dict[EventStatus, set[EventStatus]] = {
    EventStatus.DETECTED: {EventStatus.ACTIVE},
    # ACTIVE -> ACTIVE models a delta update (FR-EVT-004): the event stays active
    # but a new event_updates row is appended.
    EventStatus.ACTIVE: {
        EventStatus.ACTIVE,
        EventStatus.COMPLETED,
        EventStatus.CANCELLED,
        EventStatus.EXPIRED,
    },
    EventStatus.COMPLETED: set(),
    EventStatus.CANCELLED: set(),
    EventStatus.EXPIRED: set(),
}

DEADLINE_TRANSITIONS: dict[DeadlineState, set[DeadlineState]] = {
    DeadlineState.OPEN: {
        DeadlineState.DUE_SOON,
        DeadlineState.EXPIRED,  # due time passed with no reminder window configured
        DeadlineState.CANCELLED,
        DeadlineState.COMPLETED,
    },
    DeadlineState.DUE_SOON: {
        DeadlineState.EXPIRED,
        DeadlineState.CANCELLED,
        DeadlineState.COMPLETED,
    },
    DeadlineState.EXPIRED: set(),
    DeadlineState.CANCELLED: set(),
    DeadlineState.COMPLETED: set(),
}

ACTION_TRANSITIONS: dict[ActionStatus, set[ActionStatus]] = {
    ActionStatus.PROPOSED: {ActionStatus.WAITING_APPROVAL},
    ActionStatus.WAITING_APPROVAL: {
        ActionStatus.APPROVED,
        ActionStatus.REJECTED,
        ActionStatus.EXPIRED,
    },
    ActionStatus.APPROVED: {ActionStatus.EXECUTING},
    ActionStatus.EXECUTING: {ActionStatus.SUCCEEDED, ActionStatus.FAILED},
    ActionStatus.SUCCEEDED: set(),
    ActionStatus.REJECTED: set(),
    ActionStatus.EXPIRED: set(),
    ActionStatus.FAILED: set(),
}

# Application lifecycle (FR-MEM-004): forward = event-driven stage progress;
# the single backward step from each stage models "reversible via correction".
COMPANY_LIFECYCLE_TRANSITIONS: dict[CompanyLifecycle, set[CompanyLifecycle]] = {
    CompanyLifecycle.DISCOVERED: {CompanyLifecycle.ELIGIBLE},
    CompanyLifecycle.ELIGIBLE: {
        CompanyLifecycle.REGISTRATION,
        CompanyLifecycle.DISCOVERED,  # correction: eligibility superseded
    },
    CompanyLifecycle.REGISTRATION: {
        CompanyLifecycle.OA,
        CompanyLifecycle.ELIGIBLE,
    },
    CompanyLifecycle.OA: {
        CompanyLifecycle.SHORTLISTED,
        CompanyLifecycle.REGISTRATION,
    },
    CompanyLifecycle.SHORTLISTED: {
        CompanyLifecycle.INTERVIEW,
        CompanyLifecycle.OA,
    },
    CompanyLifecycle.INTERVIEW: {
        CompanyLifecycle.SELECTED,
        CompanyLifecycle.REJECTED,
        CompanyLifecycle.SHORTLISTED,
    },
    CompanyLifecycle.SELECTED: set(),
    CompanyLifecycle.REJECTED: set(),
}

_MACHINES: dict[str, dict] = {
    "message": MESSAGE_TRANSITIONS,
    "eligibility": ELIGIBILITY_TRANSITIONS,
    "event": EVENT_TRANSITIONS,
    "deadline": DEADLINE_TRANSITIONS,
    "action": ACTION_TRANSITIONS,
    "company_lifecycle": COMPANY_LIFECYCLE_TRANSITIONS,
}


def assert_valid_transition(machine: str, current: State, target: State) -> None:
    """Raise InvalidTransitionError unless `current -> target` is legal on `machine`."""
    try:
        transitions = _MACHINES[machine]
    except KeyError as exc:  # pragma: no cover - programming error, not runtime state
        raise ValueError(f"unknown state machine: {machine!r}") from exc

    allowed = transitions.get(current)  # type: ignore[arg-type]
    if allowed is None:
        raise InvalidTransitionError(
            f"{machine}: unknown current state {current!r}"
        )
    if target not in allowed:
        raise InvalidTransitionError(
            f"{machine}: illegal transition {current!r} -> {target!r} "
            f"(allowed: {sorted(str(s) for s in allowed) or 'none — terminal state'})"
        )


def can_transition(machine: str, current: State, target: State) -> bool:
    """Non-raising variant of assert_valid_transition."""
    try:
        assert_valid_transition(machine, current, target)
    except InvalidTransitionError:
        return False
    return True
