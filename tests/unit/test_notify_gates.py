"""Application-aware notify gates over scripted fake connections.

Covers notify_event verdicts: strict list suppression (CSV/image without the
student), negative-answer suppression, ask-whether-applied nudge with answer
buttons, APPLIED priority boost, and legacy passthrough for openings and
ungated types. No DB, no Redis, no network.
"""

import contextlib
from types import SimpleNamespace

import pytest

from pia_worker.jobs import notify as nt


class FakeResult:
    def __init__(self, value=None, mapping=None, mapping_list=None):
        self._value = value
        self._mapping = mapping
        self._list = mapping_list if mapping_list is not None else []

    def scalar(self):
        return self._value

    def mappings(self):
        return self

    def first(self):
        return self._mapping

    def all(self):
        return self._list


class FakeConn:
    def __init__(self, script):
        self._script = script
        self.executed: list[tuple[str, dict | None]] = []

    def execute(self, sql, params=None):
        text = str(sql)
        self.executed.append((text, params))
        return self._script(text, params)


class FakeEngine:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    def connect(self):
        return contextlib.nullcontext(self._conn)

    def begin(self):
        return contextlib.nullcontext(self._conn)


def _event(event_type: str, excerpt: str,
           designation: str | None = None) -> dict:
    return {
        "id": "event-uuid", "type": event_type, "deadline_at": None,
        "start_at": None, "company_id": "comp-uuid",
        "current_payload": {
            "excerpt": excerpt, "designation": designation,
            "source_message_id": "msg-1", "links": [], "venue": None,
            "salary_package": None,
        },
        "company": "Accenture", "company_key": "accenture",
        "deadline_state": None,
        "watch_state": "NONE", "group_name": "placements",
    }


def _app_row(status: str) -> dict:
    return {"id": "app-uuid", "status": status, "applied_at": None,
            "source": "test", "note": "", "opportunity_key": "accenture|",
            "updated_at": None}


def _list_payload() -> list:
    return [{"structured_payload": {"detection": {"is_candidate_list": True}}}]


def _script(event: dict, extraction_payloads=None, app_row="default"):
    def script(sql: str, params: dict | None = None) -> FakeResult:
        if "FROM events e" in sql:
            return FakeResult(mapping=event)
        if "FROM document_extractions" in sql:
            return FakeResult(mapping_list=extraction_payloads or [])
        if "eligibility_records WHERE company_id" in sql:
            return FakeResult(mapping=None)  # not eligible — gate decides
        if "FROM eligibility_records" in sql:
            return FakeResult(mapping=None)
        if "SELECT id FROM users" in sql:
            return FakeResult(mapping=SimpleNamespace(id="user-uuid"))
        if "FROM application_states" in sql:
            if app_row == "default":
                return FakeResult(mapping=None)
            return FakeResult(mapping=app_row)
        if "INSERT INTO application_states" in sql:
            return FakeResult(mapping=_app_row("UNKNOWN"))
        if "INSERT INTO notifications" in sql:
            return FakeResult(mapping=SimpleNamespace(id="notif-uuid"))
        return FakeResult()
    return script


def _run(monkeypatch: pytest.MonkeyPatch, event: dict, **kwargs) -> FakeConn:
    conn = FakeConn(_script(event, **kwargs))
    monkeypatch.setattr(nt, "_engine", lambda: FakeEngine(conn))
    monkeypatch.setattr(nt, "_enqueue_send", lambda nid: "queued")
    return conn


class TestNotifyGates:
    def test_csv_without_name_suppresses_form(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _run(monkeypatch,
                    _event("FORM", "Fill the Mars.AI account creation form."),
                    extraction_payloads=_list_payload())
        assert nt.notify_event("event-uuid") == "suppressed_list"
        ins = [p for sql, p in conn.executed
               if "INSERT INTO notifications" in sql and p]
        assert ins[0]["status"] == "SUPPRESSED"

    def test_not_applied_suppresses_post_application(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _run(
            monkeypatch,
            _event("FORM", "Candidates who applied must complete verification."),
            app_row=_app_row("NOT_APPLIED"))
        assert nt.notify_event("event-uuid") == "suppressed_state"
        assert not any("INSERT INTO actions" in sql
                       for sql, _ in conn.executed)

    def test_unknown_sends_ask_nudge_with_buttons(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _run(
            monkeypatch,
            _event("FORM", "Candidates who applied must complete verification."),
            app_row=_app_row("UNKNOWN"))
        assert nt.notify_event("event-uuid") == "asked_applied"
        ins = [p for sql, p in conn.executed
               if "INSERT INTO notifications" in sql and p]
        import json
        payload = json.loads(ins[0]["payload"])
        assert "have you applied" in payload["text"].lower()
        buttons = payload["reply_markup"]["inline_keyboard"]
        flat = [b for row in buttons for b in row]
        assert {b["callback_data"] for b in flat} == {
            "app:app-uuid:APPLIED", "app:app-uuid:NOT_APPLIED",
            "app:app-uuid:NOT_SURE", "app:app-uuid:NOT_INTERESTED"}

    def test_applied_cancellation_boosts_to_high(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _run(
            monkeypatch,
            _event("REGISTRATION",
                   "Complete verification before Friday or your application "
                   "will be cancelled."),
            app_row=_app_row("APPLIED"))
        assert nt.notify_event("event-uuid") == "queued:HIGH"
        ins = [p for sql, p in conn.executed
               if "INSERT INTO notifications" in sql and p]
        assert ins[0]["priority"] == "HIGH"

    def test_opening_still_surfaces_for_not_applied(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _run(
            monkeypatch,
            _event("REGISTRATION", "Accenture has opened applications."),
            app_row=_app_row("NOT_APPLIED"))
        assert nt.notify_event("event-uuid") == nt.DIGEST_QUEUED
        _ = conn

    def test_ungated_exam_type_untouched(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _run(
            monkeypatch,
            _event("EXAM", "Mid-sem exam schedule released."),
            app_row=_app_row("NOT_APPLIED"))
        assert nt.notify_event("event-uuid") == nt.DIGEST_QUEUED
        _ = conn

    def test_role_mismatch_suppresses(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No tracked row for (Accenture, Business Analyst) — even though the
        # student applied for SWE, the BA message must not follow up.
        conn = _run(
            monkeypatch,
            _event("FORM",
                   "Business Analyst candidates must complete verification.",
                   designation="Business Analyst"),
            app_row=None)
        assert nt.notify_event("event-uuid") == "suppressed_state"
        _ = conn
