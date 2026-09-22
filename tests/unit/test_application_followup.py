"""Application-aware follow-up decisions (spec §15 cases 1-7 + extras).

The contract: company mentioned != user applied. Only APPLIED + a matched
role + an actionable application message may follow up.
"""

from pia_shared.enums import ApplicationStatus, NotificationPriority
from pia_worker.notify.classify import (
    ApplicationMessageType,
    analyze_application_message,
    decide_application_followup,
)

ACC = ApplicationStatus


def _decide(status: ApplicationStatus, text: str,
            role_matched: bool = True):
    return decide_application_followup(
        status=status, analysis=analyze_application_message(text),
        role_matched=role_matched)


class TestSpecCases:
    def test_1_not_applied_confirmation_email_no_followup(self) -> None:
        d = _decide(ACC.NOT_APPLIED,
                    "Accenture candidates should check their confirmation email.")
        assert not d.should_follow_up
        assert not d.suggest_ask_applied

    def test_2_applied_confirmation_email_followup(self) -> None:
        d = _decide(ACC.APPLIED,
                    "Candidates who applied should check their confirmation email.")
        assert d.should_follow_up
        assert d.priority is NotificationPriority.MEDIUM

    def test_3_applied_generic_hiring_no_post_followup(self) -> None:
        d = _decide(ACC.APPLIED, "Accenture is hiring freshers.")
        assert not d.should_follow_up

    def test_4_applied_verification_or_cancelled_high(self) -> None:
        d = _decide(
            ACC.APPLIED,
            "Candidates must complete verification before Friday or their "
            "application will be cancelled.")
        assert d.should_follow_up
        assert d.priority is NotificationPriority.HIGH

    def test_5_different_role_never_matches(self) -> None:
        d = _decide(
            ACC.APPLIED,
            "Accenture Business Analyst candidates must complete verification.",
            role_matched=False)
        assert not d.should_follow_up
        assert "role" in d.reason

    def test_6_unknown_never_assumes_applied(self) -> None:
        d = _decide(ACC.UNKNOWN,
                    "Candidates who applied should check their confirmation email.")
        assert not d.should_follow_up
        assert d.suggest_ask_applied

    def test_7_company_isolation(self) -> None:
        text = "Candidates must complete verification or applications lapse."
        assert _decide(ACC.APPLIED, text).should_follow_up
        assert not _decide(ACC.NOT_APPLIED, text).should_follow_up
        assert not _decide(ACC.NOT_INTERESTED, text).should_follow_up


class TestMessageTypes:
    def test_hiring_drive_is_pre_application(self) -> None:
        a = analyze_application_message(
            "Accenture recruitment drive announced for 2027 batch.")
        assert a.message_type is ApplicationMessageType.PRE_APPLICATION
        assert not a.requires_action

    def test_interview_experience_is_generic(self) -> None:
        a = analyze_application_message(
            "My Accenture interview experience from yesterday.")
        assert a.message_type is ApplicationMessageType.GENERIC

    def test_opening_for_not_applied_stays_quiet(self) -> None:
        d = _decide(ACC.NOT_APPLIED, "Accenture has opened applications.")
        assert not d.should_follow_up
        assert not d.suggest_ask_applied  # already answered — never re-ask

    def test_form_problem_for_applied_follows_up(self) -> None:
        d = _decide(
            ACC.APPLIED,
            "Candidates who faced problems while submitting the Accenture "
            "application form should check their registered email.")
        assert d.should_follow_up

    def test_deadline_action_for_applied_follows_up(self) -> None:
        d = _decide(
            ACC.APPLIED,
            "Candidates who have applied for Accenture must complete the "
            "additional verification before the deadline.")
        assert d.should_follow_up
        assert d.priority in (NotificationPriority.HIGH,
                              NotificationPriority.MEDIUM)

    def test_eligible_not_applied_gets_ask_nudge(self) -> None:
        d = _decide(ACC.ELIGIBLE_NOT_APPLIED,
                    "After registering on the portal, did you receive the "
                    "confirmation email?")
        assert not d.should_follow_up
        assert d.suggest_ask_applied

    def test_not_sure_gets_ask_nudge(self) -> None:
        d = _decide(ACC.NOT_SURE, "Complete your document upload to confirm.")
        assert not d.should_follow_up
        assert d.suggest_ask_applied

    def test_applied_opening_is_informational(self) -> None:
        d = _decide(ACC.APPLIED,
                    "Accenture has released a new job opportunity.")
        assert not d.should_follow_up
