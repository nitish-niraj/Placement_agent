"""Context-first understanding: deadline truth, recommendations, evidence."""

from datetime import datetime

from pia_worker.notify.context import (
    AttachmentVerification,
    DeadlineStatus,
    Recommendation,
    build_context,
    deadline_status,
)
from pia_worker.notify.topics import MessageTopic

IST_NOW = datetime(2026, 9, 23, 18, 0)


def _ist(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("Asia/Kolkata"))


class TestDeadlineStatus:
    def test_passed_today_is_expired_not_today(self) -> None:
        assert deadline_status(_ist(2026, 9, 23, 0, 0), IST_NOW) is \
            DeadlineStatus.EXPIRED

    def test_future_today_is_today(self) -> None:
        assert deadline_status(_ist(2026, 9, 23, 23, 59), IST_NOW) is \
            DeadlineStatus.TODAY

    def test_under_24h_is_urgent(self) -> None:
        assert deadline_status(_ist(2026, 9, 24, 12, 0), IST_NOW) is \
            DeadlineStatus.URGENT

    def test_far_future_is_upcoming(self) -> None:
        assert deadline_status(_ist(2026, 9, 27, 13, 0), IST_NOW) is \
            DeadlineStatus.UPCOMING

    def test_missing_is_unknown(self) -> None:
        assert deadline_status(None, IST_NOW) is DeadlineStatus.UNKNOWN

    def test_naive_treated_as_ist(self) -> None:
        assert deadline_status(datetime(2026, 9, 22, 22, 0), IST_NOW) is \
            DeadlineStatus.EXPIRED


class TestRecommend:
    def _ctx(self, **over):
        kw = {"event_id": "e", "company": None,
              "subject_display": "MARS", "role": None,
              "event_type": "REGISTRATION", "topic": MessageTopic.REGISTRATION,
              "action_required": True, "eligibility_status": "UNKNOWN",
              "application_status": "UNKNOWN", "deadline": None,
              "location": None, "source_message": "register now",
              "source_message_id": "m", "source_group": "g", "reason": "r"}
        kw.update(over)
        return build_context(**kw)

    def test_defaulter_present_is_action(self) -> None:
        ctx = self._ctx(topic=MessageTopic.DEFAULTER_CHECK,
                        verification=AttachmentVerification.USER_PRESENT,
                        attachment_detail="matched registration_number")
        assert ctx.recommendation is Recommendation.ACTION_REQUIRED
        assert "defaulter list" in ctx.recommendation_text

    def test_defaulter_absent_is_no_action(self) -> None:
        ctx = self._ctx(topic=MessageTopic.DEFAULTER_CHECK,
                        verification=AttachmentVerification.USER_ABSENT)
        assert ctx.recommendation is Recommendation.NO_ACTION_REQUIRED

    def test_unavailable_is_verify_never_verdict(self) -> None:
        ctx = self._ctx(verification=AttachmentVerification.NOT_AVAILABLE)
        assert ctx.recommendation is Recommendation.VERIFY
        assert "could not be verified" in ctx.recommendation_text

    def test_expired_beats_action(self) -> None:
        ctx = self._ctx(deadline=_ist(2026, 9, 22, 22, 0), now=IST_NOW)
        assert ctx.deadline_status is DeadlineStatus.EXPIRED
        assert ctx.recommendation is Recommendation.NO_ACTION_REQUIRED

    def test_evidence_chain_recorded(self) -> None:
        ctx = self._ctx(attachments=("list.csv",),
                        verification=AttachmentVerification.USER_ABSENT,
                        attachment_detail="0 rows matched identifier")
        kinds = [r["kind"] for r in ctx.evidence_refs]
        assert kinds == ["message", "event", "attachments",
                         "attachment_verification"]
        assert "General" not in ctx.subject_display
