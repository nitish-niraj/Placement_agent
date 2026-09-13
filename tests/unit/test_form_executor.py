"""P14.1 executor policy, fully offline: flag gate, state checks, dry-run
(never transitions), success/failure bookkeeping with audits and Telegram.
The browser (_run_form) is monkeypatched — the driver itself is exercised
against a real form when one arrives."""

import contextlib
from types import SimpleNamespace

import pytest

from pia_worker.automation import executor as ex

ACTION_ID = "00000000-0000-0000-0000-00000000000a"


class FakeResult:
    def __init__(self, value=None, mapping=None):
        self._value = value
        self._mapping = mapping

    def scalar(self):
        return self._value

    def mappings(self):
        return self

    def first(self):
        return self._mapping


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
        self.conn = conn

    def connect(self):
        return contextlib.nullcontext(self.conn)

    def begin(self):
        return contextlib.nullcontext(self.conn)


def _action_row(status: str = "APPROVED", type_: str = "form_draft") -> dict:
    return {"id": ACTION_ID, "type": type_, "status": status,
            "target": "https://docs.google.com/forms/d/e/ABC/viewform",
            "payload": {"form_url": "https://docs.google.com/forms/d/e/ABC/viewform",
                        "presenters": ["Dr. Sharma"], "company": "SOFTLINK"}}


def _script(row: dict | None):
    def script(sql: str, _params: dict | None = None) -> FakeResult:
        if sql.strip().startswith("SELECT status"):  # transition guard query
            return FakeResult(value=row["status"] if row else None)
        if "UPDATE actions" in sql and _params:
            row["status"] = _params["status"]  # the fake state evolves
            return FakeResult()
        if "FROM actions WHERE id" in sql:  # _load_action
            return FakeResult(mapping=row)
        return FakeResult()

    return script


def _wire(monkeypatch: pytest.MonkeyPatch, row: dict | None, *,
          enabled: bool = True, dry_run: bool = False,
          run_report: dict | None = None, run_error: Exception | None = None,
          sent: list | None = None) -> FakeConn:
    monkeypatch.setattr(
        ex, "get_settings",
        lambda: SimpleNamespace(form_automation_enabled=enabled,
                                form_submit_dry_run=dry_run,
                                pia_encryption_key=""))
    conn = FakeConn(_script(row))
    monkeypatch.setattr(ex, "_engine_for_current_host", lambda: FakeEngine(conn))
    monkeypatch.setattr(ex, "_read_prefill", lambda engine: None)

    def fake_run(form_url, bag, submit_allowed):
        if run_error:
            raise run_error
        return run_report or {"filled": ["Your Name"], "skipped": [],
                              "submitted": submit_allowed, "screenshot": b"png",
                              "note": ""}

    monkeypatch.setattr(ex, "_run_form", fake_run)
    def stub(**kw):
        if sent is not None:
            sent.append(kw)
        return True

    monkeypatch.setattr(ex, "_telegram_send", stub)
    return conn


class TestPolicy:
    def test_disabled_flag_returns_before_anything(self, monkeypatch) -> None:
        conn = _wire(monkeypatch, _action_row(), enabled=False)
        assert ex.submit_form_action(ACTION_ID) == "disabled"
        assert not any("UPDATE actions" in sql for sql, _ in conn.executed)

    def test_only_approved_form_drafts_run(self, monkeypatch) -> None:
        _wire(monkeypatch, _action_row(status="WAITING_APPROVAL"))
        assert ex.submit_form_action(ACTION_ID) == "skipped_state"
        _wire(monkeypatch, _action_row(type_="other"))
        assert ex.submit_form_action(ACTION_ID) == "skipped_type"

    def test_dry_run_never_transitions(self, monkeypatch) -> None:
        sent: list = []
        conn = _wire(monkeypatch, _action_row(), dry_run=True, sent=sent)
        assert ex.submit_form_action(ACTION_ID) == "dry_run"
        assert not any("UPDATE actions" in sql for sql, _ in conn.executed)
        assert any("Dry run" in str(kw.get("text", "")) for kw in sent)

    def test_success_walks_machine_and_audits(self, monkeypatch) -> None:
        conn = _wire(monkeypatch, _action_row())
        assert ex.submit_form_action(ACTION_ID) == "submitted"
        updates = [p for sql, p in conn.executed
                   if "UPDATE actions" in sql and p]
        assert [u["status"] for u in updates] == ["EXECUTING", "SUCCEEDED"]
        audits = [p for sql, p in conn.executed if "audit_logs" in sql and p]
        assert {a["action"] for a in audits} == {"action.executing",
                                                 "action.succeeded"}

    def test_unconfirmed_submit_is_failed(self, monkeypatch) -> None:
        conn = _wire(monkeypatch, _action_row(),
                     run_report={"filled": ["Your Name"],
                                 "skipped": ["Feedback (skipped_unmappable)"],
                                 "submitted": False, "screenshot": b"png",
                                 "note": "submission not confirmed"})
        assert ex.submit_form_action(ACTION_ID) == "failed"
        updates = [p for sql, p in conn.executed
                   if "UPDATE actions" in sql and p]
        assert [u["status"] for u in updates] == ["EXECUTING", "FAILED"]

    def test_browser_error_is_failed_with_manual_fallback(
        self, monkeypatch
    ) -> None:
        sent: list = []
        conn = _wire(monkeypatch, _action_row(), run_error=RuntimeError("boom"),
                     sent=sent)
        assert ex.submit_form_action(ACTION_ID) == "failed"
        updates = [p for sql, p in conn.executed
                   if "UPDATE actions" in sql and p]
        assert [u["status"] for u in updates] == ["EXECUTING", "FAILED"]
        assert any("fill it manually" in str(kw.get("text", "")).lower()
                   for kw in sent)

    def test_dry_run_error_reports_without_transition(
        self, monkeypatch
    ) -> None:
        sent: list = []
        conn = _wire(monkeypatch, _action_row(), dry_run=True,
                     run_error=RuntimeError("boom"), sent=sent)
        assert ex.submit_form_action(ACTION_ID) == "failed"
        assert not any("UPDATE actions" in sql for sql, _ in conn.executed)
        assert any("fill it manually" in str(kw.get("text", "")).lower()
                   for kw in sent)
