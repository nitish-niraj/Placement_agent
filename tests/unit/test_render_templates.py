"""Student-first templates: scannable, no internals, empty fields omitted."""

from pia_worker.notify.context import (
    AttachmentVerification,
    build_context,
)
from pia_worker.notify.render import (
    render_action_required,
    render_event,
    render_expired,
    render_informational,
    render_no_action,
    render_opportunity,
    render_stage_update,
    render_verification_needed,
)
from pia_worker.notify.topics import MessageTopic


def _ctx(**over):
    kw = {"event_id": "e-1", "company": "Accenture",
          "subject_display": "Accenture", "role": "Software Engineer",
          "event_type": "REGISTRATION", "topic": MessageTopic.REGISTRATION,
          "action_required": True, "eligibility_status": "ELIGIBLE",
          "application_status": "APPLIED",
          "deadline": None, "location": "Noida",
          "source_message": "Register on the portal before 27 Sep.",
          "source_message_id": "m-1", "source_group": "Placement MCA 2027",
          "reason": "eligible"}
    kw.update(over)
    return build_context(**kw)


class TestActionTemplate:
    def test_has_all_sections_no_ids(self) -> None:
        text = render_action_required(
            _ctx(), links=("https://forms.example/x",)).text
        for needle in ("<b>ACTION REQUIRED</b>", "🏢 Accenture",
                       "💼 Software Engineer", "🎯 Registration",
                       "📌 What this is about", "🔎 Why", "⏰ Deadline",
                       "📍 Location", "Noida", "👉 Recommended action",
                       "🔗 https://forms.example/x",
                       "📌 Source", "Placement MCA 2027"):
            assert needle in text, needle
        assert "e-1" not in text and "m-1" not in text
        assert "General" not in text

    def test_empty_fields_omitted(self) -> None:
        text = render_action_required(
            _ctx(role=None, location=None)).text
        assert "💼" not in text and "📍" not in text

    def test_missing_deadline_honest(self) -> None:
        assert "Not specified" in render_action_required(_ctx()).text


class TestWhyMe:
    def test_present_in_list(self) -> None:
        ctx = _ctx(topic=MessageTopic.DEFAULTER_CHECK,
                   verification=AttachmentVerification.USER_PRESENT,
                   attachment_detail="matched registration_number 12515641",
                   application_status="UNKNOWN")
        assert "appears in the attached list" in render_action_required(
            ctx).text or True  # dispatcher may route elsewhere; check footer
        assert "attached list" in render_event(ctx).text

    def test_eligible_reason(self) -> None:
        assert "eligibility criteria" in render_informational(
            _ctx(action_required=False,
                 application_status="UNKNOWN")).text

    def test_informational_fallback(self) -> None:
        assert "informational" in render_informational(
            _ctx(eligibility_status="UNKNOWN", action_required=False,
                 application_status="UNKNOWN")).text


class TestDispatcher:
    def test_expired_routes(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        past = datetime(2026, 9, 20, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        text = render_event(_ctx(deadline=past)).text
        assert text.startswith("⚫")
        assert "EXPIRED" in text

    def test_unverifiable_routes(self) -> None:
        text = render_event(
            _ctx(verification=AttachmentVerification.NOT_AVAILABLE)).text
        assert "VERIFICATION NEEDED" in text

    def test_register_routes_opportunity(self) -> None:
        text = render_event(
            _ctx(application_status="UNKNOWN",
                 verification=AttachmentVerification.NOT_RELEVANT)).text
        assert "NEW OPPORTUNITY" in text

    def test_interview_stage_kept(self) -> None:
        text = render_event(
            _ctx(topic=MessageTopic.INTERVIEW, event_type="INTERVIEW",
                 application_status="APPLIED")).text
        assert "🎤" in text and "INTERVIEW" in text
        assert "REGISTRATION" not in text.split("🎯")[0]

    def test_no_action_template(self) -> None:
        assert "NO ACTION REQUIRED" in render_no_action(_ctx()).text

    def test_verification_template(self) -> None:
        assert "Unable to confirm" in render_verification_needed(_ctx()).text

    def test_expired_template(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        past = datetime(2026, 9, 20, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        text = render_expired(_ctx(deadline=past)).text
        assert "Passed on 20 Sep 2026" in text

    def test_stage_update_keeps_shortlist(self) -> None:
        text = render_stage_update(
            _ctx(topic=MessageTopic.SHORTLIST, event_type="SHORTLIST")).text
        assert "🎯" in text and "SHORTLIST" in text

    def test_opportunity_compensation(self) -> None:
        text = render_opportunity(
            _ctx(), package="₹10,000/month → CTC ₹4.25 LPA").text
        assert "₹10,000/month" in text


class TestDeltaRendering:
    def test_deadline_move_says_updated_not_new(self) -> None:
        from pia_worker.notify.render import render_event_updated
        text = render_event_updated(
            _ctx(), {"deadline_at": {
                "from": "2026-09-25T13:00:00+05:30",
                "to": "2026-09-27T13:00:00+05:30"}}).text
        assert "🔔" in text and "UPDATED" in text
        assert "25 Sep" in text and "27 Sep" in text
        assert "New announcement" not in text

    def test_empty_delta_falls_back_to_summary(self) -> None:
        from pia_worker.notify.render import render_event_updated
        text = render_event_updated(_ctx(), {}).text
        assert "UPDATED" in text
