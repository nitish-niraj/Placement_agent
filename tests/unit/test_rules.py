"""Rule-based classifier contract (FR-CLS-001..004, FR-CLS-002 independence)."""

from pia_shared.enums import GroupCategory, Importance, MsgDomain
from pia_worker.ai.rules import classify_rules, eligible_list_signal


def test_critical_deadline_today() -> None:
    domain, importance = classify_rules(
        "Accenture OA registration closes today 6 PM - eligible candidates check portal now"
    )
    assert domain is MsgDomain.PLACEMENT
    assert importance is Importance.CRITICAL


def test_exam_with_date_is_critical_regardless_of_placement_group() -> None:
    """FR-CLS-002 acceptance: non-placement academic deadline can be CRITICAL."""
    _, importance = classify_rules(
        "Mid-sem exam today 11 AM in Seminar Hall 2", GroupCategory.PLACEMENT
    )
    assert importance is Importance.CRITICAL


def test_greeting_is_ignored() -> None:
    for text in ("Good morning everyone", "thanks bro 🙏", "ok"):
        domain, importance = classify_rules(text)
        assert importance is Importance.IGNORE
        assert domain is MsgDomain.GENERAL


def test_eligible_list_is_high_placement() -> None:
    domain, importance = classify_rules(
        "Wipro drive eligible list attached. Check your registration numbers in the PDF."
    )
    assert domain is MsgDomain.PLACEMENT
    assert importance is Importance.HIGH


def test_fee_notice_is_administrative_medium() -> None:
    domain, importance = classify_rules(
        "Even semester exam fee payment window is open on the portal."
    )
    assert domain is MsgDomain.ADMINISTRATIVE
    assert importance is Importance.MEDIUM


def test_exam_ca_detected() -> None:
    domain, _ = classify_rules("CAP470 CA-2 on Monday 11 AM, syllabus units 3-5")
    assert domain is MsgDomain.EXAMINATION


def test_group_category_priors_fill_gaps() -> None:
    domain, _ = classify_rules(
        "please check the updated sheet", GroupCategory.PLACEMENT
    )
    assert domain is MsgDomain.PLACEMENT


def test_empty_text_is_unknown_ignore() -> None:
    domain, importance = classify_rules("")
    assert domain is MsgDomain.UNKNOWN
    assert importance is Importance.IGNORE


def test_eligible_list_signal() -> None:
    assert eligible_list_signal("Eligible candidates for the Wipro drive are attached")
    assert eligible_list_signal("SHORTLISTED STUDENTS for OA")
    assert not eligible_list_signal("Good morning everyone")
