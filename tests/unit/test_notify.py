"""P10 notification engine — F-023 decisioning, §11.1 template, FR-NOT-005
escalation windows, F-025 digest composition. All pure; delivery is stubbed."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pia_shared.enums import EventType, NotificationPriority
from pia_worker.notify.channel import DeliveryError, TelegramChannel
from pia_worker.notify.decide import (
    EventContext,
    dedup_key,
    material_state_hash,
    priority_for_event,
    reminder_windows,
    windows_crossed,
)
from pia_worker.notify.render import render_digest, render_event_alert

NOW = datetime(2026, 9, 6, 19, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


def ctx(**overrides) -> EventContext:
    base = dict(
        event_id="e1", event_type=EventType.OA, has_company=True,
        company_eligible=True, company_watching=True,
    )
    base.update(overrides)
    return EventContext(**base)


class TestPriorityLadder:
    def test_kyc_is_critical(self) -> None:
        d = priority_for_event(ctx(event_type=EventType.KYC), NOW)
        assert d.priority is NotificationPriority.CRITICAL
        assert d.delivery == "immediate"

    def test_deadline_today_is_critical(self) -> None:
        d = priority_for_event(
            ctx(event_type=EventType.REGISTRATION,
                deadline_at=NOW.replace(hour=23, minute=0)), NOW)
        assert d.priority is NotificationPriority.CRITICAL
        assert "TODAY" in d.reason

    def test_deadline_today_for_eligible_company_names_action(self) -> None:
        d = priority_for_event(ctx(deadline_at=NOW.replace(hour=23, minute=0)), NOW)
        assert d.priority is NotificationPriority.CRITICAL
        assert "eligible" in d.reason

    def test_start_within_24h_is_critical(self) -> None:
        d = priority_for_event(ctx(start_at=NOW + timedelta(hours=9)), NOW)
        assert d.priority is NotificationPriority.CRITICAL

    def test_eligible_company_event_is_high(self) -> None:
        d = priority_for_event(ctx(deadline_at=NOW + timedelta(days=3)), NOW)
        assert d.priority is NotificationPriority.HIGH
        assert d.delivery == "immediate"

    def test_registration_deadline_over_24h_is_high_without_eligibility(self) -> None:
        d = priority_for_event(
            ctx(company_eligible=False, company_watching=False,
                deadline_at=NOW + timedelta(days=3)), NOW)
        assert d.priority is NotificationPriority.HIGH

    def test_academic_exam_is_medium_digest(self) -> None:
        d = priority_for_event(
            ctx(event_type=EventType.EXAM, company_eligible=False,
                company_watching=False, has_company=False), NOW)
        assert d.priority is NotificationPriority.MEDIUM
        assert d.delivery == "digest"
        assert "FR-NOT-003" in d.reason

    def test_venue_note_is_low(self) -> None:
        d = priority_for_event(
            ctx(event_type=EventType.VENUE, company_eligible=False,
                company_watching=False), NOW)
        assert d.priority is NotificationPriority.LOW
        assert d.delivery == "digest"


class TestDedupKeys:
    def test_same_state_same_key(self) -> None:
        a = dedup_key("e1", material_state_hash(None, None, "29-402", ["http://x"]))
        b = dedup_key("e1", material_state_hash(None, None, "29-402", ["http://x"]))
        assert a == b

    def test_material_delta_changes_key(self) -> None:
        a = dedup_key("e1", material_state_hash(None, None, "29-402", []))
        b = dedup_key("e1", material_state_hash(None, None, "31-101", []))
        assert a != b

    def test_escalation_windows_are_distinct_keys(self) -> None:
        state = material_state_hash(None, None, None, [])
        assert dedup_key("e1", state, "escalation", 24) != \
            dedup_key("e1", state, "escalation", 6)


class TestEscalationWindows:
    def test_critical_gets_all_windows(self) -> None:
        assert reminder_windows(NotificationPriority.CRITICAL) == (24, 6, 1)

    def test_high_gets_24h_only(self) -> None:
        assert reminder_windows(NotificationPriority.HIGH) == (24,)

    def test_medium_low_get_no_reminders(self) -> None:
        assert reminder_windows(NotificationPriority.MEDIUM) == ()
        assert reminder_windows(NotificationPriority.LOW) == ()

    def test_windows_crossed(self) -> None:
        due = NOW + timedelta(hours=5)
        crossed = windows_crossed(due, NOW, (24, 6, 1), already_sent=(24,))
        assert crossed == (6,)  # 24h already fired; 5h remaining is inside 6h only

    def test_all_windows_reach_one_hour(self) -> None:
        due = NOW + timedelta(minutes=45)
        assert windows_crossed(due, NOW, (24, 6, 1), already_sent=(24, 6)) == (1,)

    def test_window_fires_once_only(self) -> None:
        due = NOW + timedelta(hours=5)
        assert windows_crossed(due, NOW, (24, 6, 1), already_sent=(24, 6, 1)) == ()

    def test_past_due_not_crossed(self) -> None:
        assert windows_crossed(NOW - timedelta(hours=1), NOW, (24,), ()) == ()


class TestTemplateContract:
    def test_event_alert_has_all_s111_fields(self) -> None:
        alert = render_event_alert(
            priority=NotificationPriority.HIGH, company="Accenture",
            event_type="OA", what_changed="OA scheduled", why_me="You are eligible",
            deadline_at=None, start_at=NOW + timedelta(days=1),
            venue="29-402", source_group="Placement MCA 2027",
            source_excerpt="OA <b>link</b> & details", source_message_id="m-1",
            event_id="e-1",
        )
        for field in ("What changed:", "Why it matters to you:", "Deadline/time:",
                      "Action:", "Source:", "🟠"):
            assert field in alert.text
        assert "29-402" in alert.text
        assert "&lt;b&gt;" in alert.text  # HTML-escaped (never raw markup)
        assert any(e["kind"] == "message" and e["ref"] == "m-1"
                   for e in alert.evidence_refs)

    def test_missing_deadline_is_explicit_not_invented(self) -> None:
        alert = render_event_alert(
            priority=NotificationPriority.MEDIUM, company=None, event_type="EXAM",
            what_changed="exam announced", why_me="academic (FR-NOT-003)",
            deadline_at=None, start_at=None, venue=None, source_group=None,
            source_excerpt=None, source_message_id=None, event_id="e-2",
        )
        assert "unknown" in alert.text


class TestDigest:
    def test_empty_digest_is_suppressed(self) -> None:
        assert render_digest([]) is None
        assert render_digest([("Topic", [])]) is None

    def test_digest_groups_by_topic(self) -> None:
        text = render_digest([
            ("Accenture · OA", ["Deadline announced"]),
            ("Academic · Exam", ["CAPP401 rescheduled"]),
        ])
        assert text is not None
        assert "PIA daily digest" in text
        assert text.count("•") == 2


class TestChannel:
    def test_missing_config_raises_delivery_error(self) -> None:
        channel = TelegramChannel(bot_token="")
        try:
            channel.send("123", "hello")
            raised = False
        except DeliveryError:
            raised = True
        assert raised
