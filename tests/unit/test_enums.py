"""Enum values mirror the Postgres enums in 05_Backend_Schema §2 exactly."""

from pia_shared.enums import (
    DeadlineState,
    EligibilityState,
    EventStatus,
    EventType,
    Importance,
    MatchMethod,
    MatchStatus,
    MessageState,
    MsgDomain,
    NotificationStatus,
)


def test_message_states_match_master_10_1() -> None:
    assert [s.value for s in MessageState] == [
        "RECEIVED", "VALIDATED", "QUEUED", "PROCESSING", "PROCESSED", "FAILED", "RETRYING",
    ]


def test_domains_and_importance_are_independent() -> None:
    """FR-CLS-002: importance is a separate axis from domain."""
    assert [d.value for d in MsgDomain] == [
        "PLACEMENT", "ACADEMIC", "EXAMINATION", "ADMINISTRATIVE", "EVENT", "GENERAL", "UNKNOWN",
    ]
    assert [i.value for i in Importance] == ["CRITICAL", "HIGH", "MEDIUM", "LOW", "IGNORE"]


def test_eligibility_states_match_master_10_2() -> None:
    assert {s.value for s in EligibilityState} == {
        "UNKNOWN", "MATCHED", "NOT_FOUND", "AMBIGUOUS", "USER_CONFIRMED",
        "ELIGIBLE", "NOT_ELIGIBLE", "EXPIRED", "SUPERSEDED",
    }


def test_match_contract_values() -> None:
    assert [s.value for s in MatchStatus] == ["MATCHED", "NOT_FOUND", "AMBIGUOUS", "INVALID_SOURCE"]
    assert [m.value for m in MatchMethod] == [
        "EXACT", "NORMALIZED", "IDENTIFIER", "FUZZY", "MODEL_REVIEW",
    ]


def test_event_types_match_fr_evt_001() -> None:
    assert {t.value for t in EventType} == {
        "REGISTRATION", "FORM", "KYC", "OA", "EXAM", "INTERVIEW", "SHORTLIST",
        "RESULT", "VENUE", "DOCUMENT_SUBMISSION", "JOINING", "OTHER",
    }


def test_event_and_deadline_states() -> None:
    assert [s.value for s in EventStatus] == [
        "DETECTED", "ACTIVE", "COMPLETED", "CANCELLED", "EXPIRED",
    ]
    assert [s.value for s in DeadlineState] == [
        "OPEN", "DUE_SOON", "EXPIRED", "CANCELLED", "COMPLETED",
    ]
    assert [s.value for s in NotificationStatus] == [
        "PENDING", "QUEUED", "SENT", "FAILED", "PENDING_DELIVERY", "SUPPRESSED",
    ]
