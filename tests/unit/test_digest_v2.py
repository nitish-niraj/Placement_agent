"""Categorized digest: meaningful lines per category, one send per day."""

import contextlib
from types import SimpleNamespace

from pia_worker.jobs import notify as nt
from pia_worker.notify.render import (
    digest_category,
    render_digest_v2,
)


def _item(priority="MEDIUM", subject="Accenture", stage="Registration",
          topic="REGISTRATION", deadline="2026-09-27T13:00:00+05:30",
          rec="Register before the deadline if you want to participate.",
          company="Accenture", reason="r"):
    return {"id": "n-1", "priority": priority,
            "payload": {"subject": subject, "stage": stage, "topic": topic,
                        "deadline": deadline, "recommendation_text": rec,
                        "reason": reason},
            "reason": reason, "event_type": "REGISTRATION", "company": company}


class TestCategorize:
    def test_high_goes_action(self) -> None:
        assert digest_category(_item(priority="HIGH")) == "action"

    def test_interview_stage(self) -> None:
        assert digest_category(_item(priority="LOW", stage="Interview")) == \
            "interviews"

    def test_test_stage(self) -> None:
        assert digest_category(_item(priority="LOW", stage="Online Test")) == \
            "tests"

    def test_deadline_fallback(self) -> None:
        assert digest_category(_item(priority="LOW", stage="Update",
                                     topic="OTHER")) == "deadlines"

    def test_legacy_payload_falls_to_info(self) -> None:
        assert digest_category({"id": "x", "priority": "LOW", "payload": {},
                                "reason": "Non-urgent announcement"}) == "info"


class TestRenderV2:
    def test_grouped_with_counts_no_bare_reasons(self) -> None:
        text = render_digest_v2("2026-09-23", [
            _item(), _item(stage="Interview", topic="INTERVIEW",
                           rec="Attend as scheduled."),
            _item(priority="LOW", stage="Update", deadline=None,
                  rec="FYI only."),
        ])
        assert text is not None
        assert "PIA DAILY PLACEMENT DIGEST" in text
        assert "2026-09-23" in text
        assert "📋 REGISTRATIONS" in text and "🎤 INTERVIEWS" in text
        assert "Non-urgent announcement" not in text
        assert "27 Sep" in text

    def test_empty_is_none(self) -> None:
        assert render_digest_v2("2026-09-23", []) is None


class FakeResult:
    def __init__(self, mapping=None, mapping_list=None):
        self._mapping = mapping
        self._list = mapping_list or []

    def mappings(self):
        return self

    def first(self):
        return self._mapping

    def all(self):
        return self._list


class FakeConn:
    def __init__(self, script):
        self._script = script
        self.executed: list = []

    def execute(self, sql, params=None):
        text = str(sql)
        self.executed.append((text, params))
        return self._script(text, params)


class FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return contextlib.nullcontext(self._conn)

    def begin(self):
        return contextlib.nullcontext(self._conn)


def _settings(**over):
    base = {"digest_enabled": True, "notification_enabled": True,
            "app_timezone": "Asia/Kolkata", "telegram_bot_token": "t",
            "telegram_chat_id": "c"}
    base.update(over)
    return SimpleNamespace(**base)


class FakeChannel:
    sent: list = []

    def send(self, chat_id: str, text: str, reply_markup=None):
        FakeChannel.sent.append(text)
        from pia_worker.notify.channel import DeliveryResult
        return DeliveryResult(True, "telegram")


def _run(monkeypatch, items, claimed: bool, **settings_over):
    def script(sql: str, params: dict | None):
        if "INSERT INTO idempotency_keys" in sql:
            return FakeResult(
                mapping={"key": "k"} if claimed else None)
        if "FROM notifications n" in sql:
            return FakeResult(mapping_list=items)
        return FakeResult()

    conn = FakeConn(script)
    monkeypatch.setattr(nt, "_engine", lambda: FakeEngine(conn))
    monkeypatch.setattr(nt, "get_settings",
                        lambda: _settings(**settings_over))
    monkeypatch.setattr(nt, "_channel", lambda: FakeChannel())
    FakeChannel.sent = []
    return conn


class TestDigestIdempotency:
    def test_first_run_sends(self, monkeypatch) -> None:
        _run(monkeypatch, [_item()], claimed=True)
        assert nt.daily_digest() == "sent:1"
        assert len(FakeChannel.sent) == 1
        assert "REGISTRATIONS" in FakeChannel.sent[0]

    def test_second_run_same_day_skips(self, monkeypatch) -> None:
        _run(monkeypatch, [_item()], claimed=False)
        assert nt.daily_digest() == "duplicate"
        assert FakeChannel.sent == []

    def test_empty_suppressed(self, monkeypatch) -> None:
        _run(monkeypatch, [], claimed=True)
        assert nt.daily_digest() == "empty"

    def test_disabled_flag(self, monkeypatch) -> None:
        _run(monkeypatch, [_item()], claimed=True, digest_enabled=False)
        assert nt.daily_digest() == "disabled"
        assert FakeChannel.sent == []
