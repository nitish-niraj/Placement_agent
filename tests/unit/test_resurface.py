"""Stuck-notification triage: re-decide through today's pipeline.

QUEUED/PENDING_DELIVERY rows are re-rendered when still live, routed to the
digest when downgraded, and buried (never resent verbatim) when expired,
dead, or gate-suppressed. DB/Redis/network all faked.
"""

import contextlib
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pia_worker.jobs import notify as nt

IST = ZoneInfo("Asia/Kolkata")


class FakeResult:
    def __init__(self, value=None, mapping=None, mapping_list=None,
                 rowcount=1):
        self._value = value
        self._mapping = mapping
        self._list = mapping_list or []
        self.rowcount = rowcount

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


def _note(priority="HIGH", status="QUEUED", outcome="created"):
    return {"id": "notif-1", "priority": priority, "event_id": "event-1",
            "payload": {"outcome": outcome}, "reason": "old",
            "status": status}


def _event(deadline=None, event_status="ACTIVE",
           excerpt="Register on the portal before the deadline.",
           etype="REGISTRATION"):
    return {"id": "event-1", "type": etype, "deadline_at": deadline,
            "start_at": None, "company_id": "comp-1",
            "current_payload": {"excerpt": excerpt, "links": [],
                                "source_message_id": "msg-1"},
            "company": "Acme", "company_key": "acme",
            "deadline_state": None, "watch_state": "NONE",
            "group_name": "g"}


def _script(notes, event=None, event_status="ACTIVE", eligible=True,
            app_row=None):
    def script(sql: str, params: dict | None):
        if "FROM notifications n" in sql:
            return FakeResult(mapping_list=notes)
        if "FROM events e" in sql:
            return FakeResult(mapping=event)
        if "SELECT status::text AS status FROM events" in sql:
            return FakeResult(
                mapping=SimpleNamespace(status=event_status))
        if "eligibility_records WHERE company_id" in sql:
            return FakeResult(
                mapping=SimpleNamespace(x=1) if eligible else None)
        if "FROM document_extractions" in sql:
            return FakeResult(mapping_list=[])
        if "SELECT id FROM users" in sql:
            return FakeResult(mapping=SimpleNamespace(id="user-1"))
        if "FROM application_states" in sql:
            return FakeResult(mapping=app_row)
        if "FROM attachments a WHERE" in sql:
            return FakeResult(mapping_list=[])
        if "FROM event_updates" in sql:
            return FakeResult(mapping=None)
        if sql.strip().startswith("UPDATE notifications"):
            return FakeResult(rowcount=1)
        return FakeResult()
    return script


def _run(monkeypatch, notes, **kw):
    conn = FakeConn(_script(notes, **kw))
    monkeypatch.setattr(nt, "_engine", lambda: FakeEngine(conn))
    enqueued: list = []
    monkeypatch.setattr(nt, "_enqueue_send",
                        lambda nid: enqueued.append(nid) or "queued")
    return conn, enqueued


FUTURE = datetime(2030, 6, 1, 12, 0, tzinfo=IST)
PAST = datetime(2020, 9, 9, 12, 0, tzinfo=IST)


class TestResurface:
    def test_live_high_is_rebuilt_and_requeued(
            self, monkeypatch) -> None:
        conn, enqueued = _run(
            monkeypatch, [_note()],
            event=_event(deadline=FUTURE), app_row=None)
        assert nt.resurface_stuck_notifications() == {
            "checked": 1, "resent": 1, "to_digest": 0, "superseded": 0}
        assert enqueued == ["notif-1"]
        updates = [p for sql, p in conn.executed
                   if "UPDATE notifications SET priority" in sql and p]
        assert updates and "ACTION" not in updates[0]["reason"]
        assert "Registration" in updates[0]["reason"] or \
            "Register" in updates[0]["reason"]

    def test_expired_is_buried_never_resent(
            self, monkeypatch) -> None:
        conn, enqueued = _run(
            monkeypatch, [_note()],
            event=_event(deadline=PAST))
        assert nt.resurface_stuck_notifications()["superseded"] == 1
        assert enqueued == []
        marks = [p for sql, p in conn.executed
                 if "UPDATE notifications SET status" in sql and p]
        assert marks and marks[0]["s"] == "SUPPRESSED"

    def test_cancelled_event_buried(self, monkeypatch) -> None:
        _, enqueued = _run(
            monkeypatch, [_note()],
            event=_event(deadline=FUTURE), event_status="CANCELLED")
        assert nt.resurface_stuck_notifications()["superseded"] == 1
        assert enqueued == []

    def test_gate_suppressed_buried(self, monkeypatch) -> None:
        app = {"id": "a", "status": "NOT_APPLIED", "applied_at": None,
               "source": "t", "note": "", "opportunity_key": "acme|",
               "updated_at": None}
        _, enqueued = _run(
            monkeypatch, [_note()],
            event=_event(deadline=FUTURE,
                         excerpt="Candidates who applied must complete "
                                 "verification.",
                         etype="FORM"),
            app_row=app)
        assert nt.resurface_stuck_notifications()["superseded"] == 1
        assert enqueued == []

    def test_dry_run_writes_nothing(self, monkeypatch) -> None:
        conn, enqueued = _run(
            monkeypatch, [_note()],
            event=_event(deadline=FUTURE))
        assert nt.resurface_stuck_notifications(dry_run=True) == {
            "checked": 1, "resent": 1, "to_digest": 0, "superseded": 0}
        assert enqueued == []
        assert not any(sql.strip().startswith("UPDATE")
                       for sql, _ in conn.executed)

    def test_empty_is_zero(self, monkeypatch) -> None:
        _run(monkeypatch, [])
        assert nt.resurface_stuck_notifications() == {
            "checked": 0, "resent": 0, "to_digest": 0, "superseded": 0}
