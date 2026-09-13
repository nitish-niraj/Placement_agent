"""ADR-013 Stage 3 executors, fully offline: type dispatch, the Telegram note,
the event correction (field allowlist + audit), the re-parse re-enqueue, and
the flag/state gates. The RQ enqueue and Telegram send are monkeypatched."""

import contextlib
import json
from types import SimpleNamespace

import pytest

from pia_worker.executors import proposal_executors as pe

ACTION_ID = "00000000-0000-0000-0000-00000000000a"


class FakeResult:
    def __init__(self, value=None, mapping=None):
        self._v = value
        self._m = mapping

    def scalar(self):
        return self._v

    def mappings(self):
        return self

    def first(self):
        return self._m


class FakeConn:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.executed: list[tuple[str, dict | None]] = []

    def execute(self, sql, params=None):
        text = str(sql)
        self.executed.append((text, params))
        if text.strip().startswith("SELECT status"):
            return FakeResult(value=self.state)
        if "FROM actions WHERE id" in text:
            return FakeResult(mapping=self.row)
        if "DELETE FROM idempotency_keys" in text and self._results:
            return self._results.pop(0)
        if "SELECT current_payload" in text:
            return FakeResult(mapping={"current": None, "exists_": True})
        return FakeResult()


class FakeEngine:
    def __init__(self, conn: FakeConn):
        self.conn = conn

    def connect(self):
        return contextlib.nullcontext(self.conn)

    def begin(self):
        return contextlib.nullcontext(self.conn)


def _row(type_: str, status: str = "APPROVED", payload: dict | None = None) -> dict:
    return {"id": ACTION_ID, "type": type_, "status": status,
            "target": "event:abc", "payload": payload or {}}


def _wire(monkeypatch: pytest.MonkeyPatch, row: dict | None, *,
          enabled: bool = True, sent: list | None = None,
          enqueued: list | None = None, results: list | None = None) -> FakeConn:
    conn = FakeConn(results)
    conn.row = row
    conn.state = row["status"] if row else "APPROVED"

    real_execute = conn.execute

    def execute(sql, params=None):
        text = str(sql)
        result = real_execute(sql, params)
        if "UPDATE actions" in text and params:  # the fake state evolves
            conn.state = params["status"]
        return result

    conn.execute = execute  # type: ignore[method-assign]
    monkeypatch.setattr(
        pe, "get_settings",
        lambda: SimpleNamespace(stage3_executors_enabled=enabled,
                                redis_url="redis://x"))
    monkeypatch.setattr(pe, "engine_for_current_host", lambda: FakeEngine(conn))
    def stub(**kw):
        if sent is not None:
            sent.append(kw)
        return True

    monkeypatch.setattr(pe, "_telegram_send", stub)

    class FakeQueue:
        def __init__(self, *a, **k):
            pass

        def enqueue(self, path, *args):
            if enqueued is not None:
                enqueued.append((path, *args))

    import pia_worker.queue as q

    monkeypatch.setattr(q, "Queue", FakeQueue)
    return conn


class TestGates:
    def test_disabled_flag_returns_before_anything(self, monkeypatch) -> None:
        conn = _wire(monkeypatch, _row("deadline_nudge"), enabled=False)
        assert pe.run_proposal_executor(ACTION_ID) == "disabled"
        assert not any("UPDATE actions" in sql for sql, _ in conn.executed)

    def test_only_approved_known_types_run(self, monkeypatch) -> None:
        _wire(monkeypatch, _row("form_draft"))
        assert pe.run_proposal_executor(ACTION_ID) == "skipped_type"
        _wire(monkeypatch, _row("deadline_nudge", status="WAITING_APPROVAL"))
        assert pe.run_proposal_executor(ACTION_ID) == "skipped_state"


class TestTelegramNote:
    def test_nudge_sends_the_approved_card(self, monkeypatch) -> None:
        sent: list = []
        conn = _wire(monkeypatch, _row("deadline_nudge", payload={
            "title": "CODEHOOD deadline tomorrow", "reason": "due 18:30 IST"}),
            sent=sent)
        assert pe.run_proposal_executor(ACTION_ID) == "executed"
        updates = [p for sql, p in conn.executed
                   if "UPDATE actions" in sql and p]
        assert [u["status"] for u in updates] == ["EXECUTING", "SUCCEEDED"]
        assert any("CODEHOOD deadline tomorrow" in str(kw.get("text", ""))
                   for kw in sent)

    def test_kyc_reminder_is_a_message_not_automation(self, monkeypatch) -> None:
        sent: list = []
        _wire(monkeypatch, _row("kyc_reminder", payload={
            "title": "KYC session at 2 PM", "reason": "session today"}),
            sent=sent)
        assert pe.run_proposal_executor(ACTION_ID) == "executed"
        assert any("KYC session at 2 PM" in str(kw.get("text", ""))
                   for kw in sent)


class TestEventCorrection:
    def test_correction_writes_field_and_audits(self, monkeypatch) -> None:
        conn = _wire(monkeypatch, _row("verify_field", payload={
            "correction": {"event_id": "00000000-0000-0000-0000-00000000000b",
                           "field": "designation", "value": "Data Engineer"}}))
        assert pe.run_proposal_executor(ACTION_ID) == "executed"
        updates = [p for sql, p in conn.executed
                   if "UPDATE events SET current_payload" in sql and p]
        assert updates and updates[0]["value"] == "Data Engineer"
        audits = [p for sql, p in conn.executed if "event.field.set" in sql and p]
        meta = json.loads(audits[0]["meta"])
        assert meta["after"] == "Data Engineer"
        assert meta["via"] == "approved proposal"

    def test_disallowed_field_fails_loudly(self, monkeypatch) -> None:
        sent: list = []
        conn = _wire(monkeypatch, _row("verify_field", payload={
            "correction": {"event_id": "00000000-0000-0000-0000-00000000000b",
                           "field": "cgpa", "value": "4.0"}}), sent=sent)
        assert pe.run_proposal_executor(ACTION_ID) == "failed"
        updates = [p for sql, p in conn.executed
                   if "UPDATE actions" in sql and p]
        assert [u["status"] for u in updates] == ["EXECUTING", "FAILED"]
        assert any("fill manually" in str(kw.get("text", "")).lower()
                   or "failed" in str(kw.get("text", "")).lower()
                   for kw in sent)


class TestReparse:
    def test_reparse_clears_key_and_enqueues(self, monkeypatch) -> None:
        enqueued: list = []
        conn = _wire(monkeypatch, _row("data_quality", payload={
            "reparse_message_id": "00000000-0000-0000-0000-00000000000c"}),
            enqueued=enqueued,
            results=[FakeResult(
                value="events:00000000-0000-0000-0000-00000000000c")])
        assert pe.run_proposal_executor(ACTION_ID) == "executed"
        deletes = [p for sql, p in conn.executed
                   if "DELETE FROM idempotency_keys" in sql and p]
        assert deletes and deletes[0]["key"] == "events:00000000-0000-0000-0000-00000000000c"
        assert enqueued and "extract_events" in enqueued[0][0]

    def test_reparse_without_record_fails(self, monkeypatch) -> None:
        sent: list = []
        conn = _wire(monkeypatch, _row("data_quality", payload={
            "reparse_message_id": "00000000-0000-0000-0000-00000000000c"}),
            sent=sent)
        # fake returns no deleted key -> executor must fail loudly
        conn_exec = conn.executed
        assert pe.run_proposal_executor(ACTION_ID) == "failed"
        assert any("UPDATE actions" in sql for sql, _ in conn_exec)
