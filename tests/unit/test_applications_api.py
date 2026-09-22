"""Application-state API: answer recording (machine-asserted, audited),
company resolution by name, 404s, and listing. Scripted fake engine."""

import contextlib

import pytest
from fastapi.testclient import TestClient

from pia_api.main import create_app
from pia_api.routes import applications as apps_module
from pia_api.settings import Settings

TOKEN = "test-dashboard-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
COMPANY_ID = "00000000-0000-0000-0000-0000000000c1"
APP_ID = "00000000-0000-0000-0000-0000000000a1"


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
    monkeypatch.setattr(apps_module, "get_engine", lambda: engine)
    return engine.conn


def _company_row():
    return {"id": COMPANY_ID, "canonical_name": "Accenture",
            "normalized_key": "accenture"}


def _script_answer(current_status: str = "UNKNOWN", company=None):
    updated = {"id": APP_ID, "status": "APPLIED", "applied_at": "2026-09-20",
               "source": "dashboard", "note": "", "opportunity_key": "x",
               "updated_at": "now"}

    def script(sql: str, params: dict | None = None) -> FakeResult:
        if "FROM companies c" in sql:
            return FakeResult(mapping=company)
        if "SELECT id FROM users" in sql:
            return FakeResult(value="user-uuid")
        if "SELECT id, status FROM application_states" in sql:
            if current_status == "__missing__":
                return FakeResult(mapping=None)
            return FakeResult(mapping={"id": APP_ID,
                                       "status": current_status})
        if "INSERT INTO application_states" in sql:
            return FakeResult(mapping={"id": APP_ID, "status": "UNKNOWN"})
        if sql.strip().startswith("UPDATE application_states"):
            return FakeResult(mapping=updated)
        return FakeResult()

    return script


class TestAnswer:
    def test_records_applied_with_audit(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _wire(monkeypatch, _script_answer("UNKNOWN", _company_row()))
        resp = TestClient(create_app()).post(
            "/api/v1/applications/answer", headers=AUTH,
            json={"company": "Accenture", "role": "Software Engineer",
                  "status": "APPLIED"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "APPLIED"
        assert body["company"] == "Accenture"
        assert body["role"] == "software engineer"
        audits = [p for sql, p in conn.executed if "audit_logs" in sql and p]
        assert len(audits) == 1

    def test_unknown_company_404(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, _script_answer("UNKNOWN", None))
        resp = TestClient(create_app()).post(
            "/api/v1/applications/answer", headers=AUTH,
            json={"company": "NoSuchCorp", "status": "APPLIED"})
        assert resp.status_code == 404

    def test_missing_company_422(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, _script_answer("UNKNOWN", _company_row()))
        resp = TestClient(create_app()).post(
            "/api/v1/applications/answer", headers=AUTH,
            json={"company": "", "status": "APPLIED"})
        assert resp.status_code == 422

    def test_bad_status_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, _script_answer("UNKNOWN", _company_row()))
        resp = TestClient(create_app()).post(
            "/api/v1/applications/answer", headers=AUTH,
            json={"company": "Accenture", "status": "MAYBE"})
        assert resp.status_code == 422


class TestList:
    def test_lists_with_company_names(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [{"id": APP_ID, "company": "TCS", "role": "ninja",
                 "opportunity_key": "tcs|ninja", "status": "APPLIED",
                 "applied_at": "2026-09-20", "source": "telegram:button",
                 "note": "", "updated_at": "now"}]

        def script(sql: str, params: dict | None = None) -> FakeResult:
            assert "FROM application_states s" in sql
            return FakeResult(rows=rows)

        _wire(monkeypatch, script)
        resp = TestClient(create_app()).get("/api/v1/applications",
                                            headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["applications"] == rows

    def test_unauthorized_without_token(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, lambda sql, params: FakeResult(rows=[]))
        resp = TestClient(create_app()).get("/api/v1/applications")
        assert resp.status_code in (401, 403)
