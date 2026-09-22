"""P13/F-030 actions API: list with pre-fill, approve/reject guarded by the
§10.4 machine (409 on illegal transitions), 404s, and audit writes. The DB
engine is a scripted fake — no Postgres is touched."""

import contextlib

import pytest
from fastapi.testclient import TestClient

from pia_api.main import create_app
from pia_api.routes import actions as actions_module
from pia_api.settings import Settings

TOKEN = "test-dashboard-token"
ACTION_ID = "00000000-0000-0000-0000-00000000000a"
ACTION_ROW = {
    "id": ACTION_ID, "type": "form_draft",
    "target": "https://docs.google.com/forms/d/e/ABC/viewform",
    "payload": {"event_id": "00000000-0000-0000-0000-00000000000b",
                "form_url": "https://docs.google.com/forms/d/e/ABC/viewform"},
    "risk_level": "medium", "status": "WAITING_APPROVAL",
    "approval_required": True, "created_at": "2026-09-13T10:00:00+00:00",
    "updated_at": "2026-09-13T10:00:00+00:00",
    "event_id": "00000000-0000-0000-0000-00000000000b",
    "event_title": "SOFTLINK — Form", "company": "SOFTLINK",
}
AUTH = {"Authorization": f"Bearer {TOKEN}"}


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
        self.executed: list[tuple[str, dict | None]] = []

    def execute(self, sql, params=None):
        text = str(sql)
        self.executed.append((text, params))
        return self._script(text, params)


class FakeEngine:
    """One shared scripted connection for every connect()/begin() call."""

    def __init__(self, script):
        self.conn = FakeConn(script)

    def connect(self):
        return contextlib.nullcontext(self.conn)

    def begin(self):
        return contextlib.nullcontext(self.conn)


def _wire(monkeypatch: pytest.MonkeyPatch, script) -> FakeConn:
    monkeypatch.setattr(
        "pia_api.settings._settings",
        Settings(_env_file=None, dashboard_token=TOKEN),  # type: ignore[call-arg]
    )
    engine = FakeEngine(script)
    monkeypatch.setattr(actions_module, "get_engine", lambda: engine)
    return engine.conn


def _client() -> TestClient:
    return TestClient(create_app())


class TestList:
    def test_list_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, lambda sql, params=None: FakeResult())
        response = _client().get("/api/v1/actions", headers=AUTH)
        assert response.status_code == 200
        assert response.json() == {"actions": []}

    def test_list_rows_with_prefill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def script(sql: str, _params: dict | None = None) -> FakeResult:
            if "FROM actions a" in sql:
                return FakeResult(rows=[ACTION_ROW])
            if "FROM candidate_profiles" in sql:
                return FakeResult(mapping={
                    "canonical_name": "Nitish Kumar", "email": "nitish@lpu.in",
                    "roll_number": "12515641", "registration_number": None,
                    "student_id": None, "branch": "CSE", "batch": "2023-2027",
                    "cgpa": 7.9, "tenth_percent": 88.0, "twelfth_percent": 84.0,
                    "backlog_count": 0, "display_name": "Nitish Kumar",
                    "mobile_number": None})
            return FakeResult()

        _wire(monkeypatch, script)
        response = _client().get("/api/v1/actions", headers=AUTH)
        assert response.status_code == 200
        body = response.json()["actions"][0]
        assert body["status"] == "WAITING_APPROVAL"
        assert body["event"]["company"] == "SOFTLINK"
        assert body["prefill"]["full_name"] == "Nitish Kumar"
        assert body["prefill"]["roll_number"] == "12515641"  # legacy plaintext

    def test_unknown_status_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, lambda sql, params=None: FakeResult())
        response = _client().get("/api/v1/actions?status=NOPE", headers=AUTH)
        assert response.status_code == 422


class TestDecide:
    def test_approve_waiting_action(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _wire(
            monkeypatch,
            lambda sql, p=None: FakeResult(
                mapping=dict(ACTION_ROW)) if "FROM actions a" in str(sql)
            else FakeResult())
        response = _client().post(f"/api/v1/actions/{ACTION_ID}/approve", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "APPROVED"
        updates = [p for sql, p in conn.executed
                   if "UPDATE actions" in sql and p]
        assert updates and updates[0]["status"] == "APPROVED"

    def test_approve_records_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _wire(
            monkeypatch,
            lambda sql, p=None: FakeResult(
                mapping=dict(ACTION_ROW)) if "FROM actions a" in str(sql)
            else FakeResult())
        _client().post(f"/api/v1/actions/{ACTION_ID}/approve", headers=AUTH)
        audits = [p for sql, p in conn.executed if "audit_logs" in sql and p]
        assert audits and audits[0]["action"] == "action.approved"

    def test_approve_from_proposed_is_409(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, lambda sql, p=None: FakeResult(
            mapping=dict(ACTION_ROW, status="PROPOSED"))
            if "FROM actions a" in str(sql) else FakeResult())
        response = _client().post(f"/api/v1/actions/{ACTION_ID}/approve", headers=AUTH)
        assert response.status_code == 409

    def test_reapprove_is_409(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, lambda sql, p=None: FakeResult(
            mapping=dict(ACTION_ROW, status="APPROVED"))
            if "FROM actions a" in str(sql) else FakeResult())
        response = _client().post(f"/api/v1/actions/{ACTION_ID}/approve", headers=AUTH)
        assert response.status_code == 409

    def test_reject_waiting_action(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, lambda sql, p=None: FakeResult(
            mapping=dict(ACTION_ROW)) if "FROM actions a" in str(sql)
            else FakeResult())
        response = _client().post(f"/api/v1/actions/{ACTION_ID}/reject", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["status"] == "REJECTED"

    def test_unknown_action_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, lambda sql, p=None: FakeResult(
            mapping=None) if "FROM actions a" in str(sql) else FakeResult())
        response = _client().post(f"/api/v1/actions/{ACTION_ID}/approve", headers=AUTH)
        assert response.status_code == 404
