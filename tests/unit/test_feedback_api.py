"""Classification feedback intake: corrections recorded with predicted-vs-
correct + audit; message and events untouched; intake listable. Fake engine."""

import contextlib

from fastapi.testclient import TestClient

from pia_api.main import create_app
from pia_api.routes import feedback as feedback_module
from pia_api.settings import Settings

TOKEN = "test-dashboard-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MESSAGE_ID = "00000000-0000-0000-0000-0000000000b1"


class FakeResult:
    def __init__(self, value=None, mapping=None, rows=None):
        self._value = value
        self._mapping = mapping
        self._rows = rows or []

    def scalar(self):
        return self._value

    def mappings(self):
        return self

    def first(self):
        return self._mapping

    def all(self):
        return self._rows


class FakeConn:
    def __init__(self, script):
        self._script = script
        self.executed: list = []

    def execute(self, sql, params=None):
        text = str(sql)
        self.executed.append((text, params))
        return self._script(text, params)


class FakeEngine:
    def __init__(self, script):
        self.conn = FakeConn(script)

    def connect(self):
        return contextlib.nullcontext(self.conn)

    def begin(self):
        return contextlib.nullcontext(self.conn)


def _wire(monkeypatch, script) -> FakeConn:
    monkeypatch.setattr(
        "pia_api.settings._settings",
        Settings(_env_file=None, dashboard_token=TOKEN),  # type: ignore[call-arg]
    )
    engine = FakeEngine(script)
    monkeypatch.setattr(feedback_module, "get_engine", lambda: engine)
    return engine.conn


def _message_row():
    return {"id": MESSAGE_ID, "domain": "GENERAL", "importance": "MEDIUM",
            "excerpt": "watcher self-test"}


def _script_answer(message=None):
    inserted = {"id": "fb-uuid", "created_at": "2026-09-23T00:00:00+00:00"}

    def script(sql: str, params: dict | None):
        if "FROM messages m" in sql:
            return FakeResult(mapping=message)
        if "SELECT id FROM users" in sql:
            return FakeResult(value="user-uuid")
        if "INSERT INTO classification_feedback" in sql:
            assert params["pdom"] == "GENERAL"
            assert params["cdom"] == "PLACEMENT"
            return FakeResult(mapping=inserted)
        return FakeResult()

    return script


class TestSubmit:
    def test_records_correction_with_audit(self, monkeypatch) -> None:
        conn = _wire(monkeypatch, _script_answer(_message_row()))
        resp = TestClient(create_app()).post(
            "/api/v1/feedback/classification", headers=AUTH,
            json={"message_id": MESSAGE_ID, "correct_domain": "PLACEMENT",
                  "correct_importance": "HIGH", "note": "actually a drive"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["predicted_domain"] == "GENERAL"
        assert body["correct_domain"] == "PLACEMENT"
        audits = [(sql, p) for sql, p in conn.executed
                  if "audit_logs" in sql and p]
        assert len(audits) == 1
        assert "classification.correction" in audits[0][0]

    def test_unknown_message_404(self, monkeypatch) -> None:
        _wire(monkeypatch, _script_answer(None))
        resp = TestClient(create_app()).post(
            "/api/v1/feedback/classification", headers=AUTH,
            json={"message_id": MESSAGE_ID, "correct_domain": "PLACEMENT"})
        assert resp.status_code == 404

    def test_bad_domain_422(self, monkeypatch) -> None:
        _wire(monkeypatch, _script_answer(_message_row()))
        resp = TestClient(create_app()).post(
            "/api/v1/feedback/classification", headers=AUTH,
            json={"message_id": MESSAGE_ID, "correct_domain": "NOPE"})
        assert resp.status_code == 422


class TestList:
    def test_lists_intake_queue(self, monkeypatch) -> None:
        rows = [{"id": "fb-uuid", "message_id": MESSAGE_ID,
                 "predicted_domain": "GENERAL", "predicted_importance": "MEDIUM",
                 "correct_domain": "PLACEMENT", "correct_importance": "HIGH",
                 "note": "", "created_at": "now", "excerpt": "watcher"}]

        def script(sql: str, params: dict | None):
            assert "FROM classification_feedback f" in sql
            return FakeResult(rows=rows)

        _wire(monkeypatch, script)
        resp = TestClient(create_app()).get(
            "/api/v1/feedback/classification", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["feedback"] == rows
