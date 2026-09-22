"""Application-state tracking: company mentioned != user applied.

Covers: the fully-connected user-driven machine, role normalization +
opportunity keys, get-or-create (pipeline may only create UNKNOWN /
ELIGIBLE_NOT_APPLIED), status recording with applied_at stamping + audit,
and the strict message list gate (CSV/XLSX/PDF/image attachment without the
student's name -> 'not_found').
"""

import contextlib

import pytest

from pia_shared.enums import ApplicationStatus
from pia_shared.states import assert_valid_transition, can_transition
from pia_worker.applications import records as rec


class FakeResult:
    def __init__(self, value=None, mapping=None, mappings_list=None):
        self._value = value
        self._mapping = mapping
        self._list = mappings_list or []

    def scalar(self):
        return self._value

    def scalar_one(self):
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


class TestMachine:
    def test_fully_connected_user_driven(self) -> None:
        for current in ApplicationStatus:
            for target in ApplicationStatus:
                assert can_transition("application", current, target)

    def test_unknown_machine_still_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown state machine"):
            assert_valid_transition("nope", ApplicationStatus.UNKNOWN,
                                    ApplicationStatus.APPLIED)


class TestKeys:
    def test_role_normalization_matches_company_keys(self) -> None:
        assert rec.normalize_role("Software Engineer") == "software engineer"
        assert rec.normalize_role("  SDE-1 (Backend) ") == "sde 1 backend"
        assert rec.normalize_role(None) == ""

    def test_opportunity_key_stable(self) -> None:
        assert rec.opportunity_key("accenture", "software engineer") == \
            "accenture|software engineer"
        assert rec.opportunity_key("tcs", "") == "tcs|"


class TestEnsureCreatesOnlyNeutralStates:
    def _script(self, sql: str, params: dict | None):
        if "FROM application_states" in sql and "SELECT" in sql:
            return FakeResult(mapping=None)  # no row yet
        if "INSERT INTO application_states" in sql:
            return FakeResult(mapping={
                "id": "app-uuid", "status": params["status"],
                "applied_at": None, "source": params["source"], "note": "",
                "opportunity_key": params["key"], "updated_at": None})
        return FakeResult()

    def test_pipeline_may_not_create_applied(self) -> None:
        conn = FakeConn(self._script)
        with pytest.raises(ValueError, match="may not create"):
            rec.ensure_application(
                conn, user_id="u", company_id="c", source="test",
                initial=ApplicationStatus.APPLIED)

    def test_ensure_unknown_ok(self) -> None:
        conn = FakeConn(self._script)
        row = rec.ensure_application(
            conn, user_id="u", company_id="c", company_normalized="acme",
            role_normalized="sde", source="test")
        assert row["id"] == "app-uuid"
        assert row["status"] == "UNKNOWN"
        assert any("audit_logs" in sql for sql, _ in conn.executed)

    def test_ensure_existing_returns_row(self) -> None:
        existing = {"id": "old", "status": "NOT_APPLIED"}
        conn = FakeConn(lambda sql, params: FakeResult(mapping=existing)
                        if "SELECT" in sql else FakeResult())
        assert rec.ensure_application(
            conn, user_id="u", company_id="c",
            source="test") == existing


class TestSetStatus:
    def _script(self, sql: str, params: dict | None):
        if "SELECT" in sql and "FROM application_states" in sql:
            return FakeResult(mapping={
                "id": "app-uuid", "status": "NOT_APPLIED",
                "applied_at": None, "source": "x", "note": "",
                "opportunity_key": "acme|sde", "updated_at": None})
        if "UPDATE application_states" in sql:
            assert params["status"] == "APPLIED"
            return FakeResult(mapping={
                "id": "app-uuid", "status": "APPLIED",
                "applied_at": "2026-09-20", "source": "telegram",
                "note": "", "opportunity_key": "acme|sde",
                "updated_at": None})
        return FakeResult()

    def test_records_answer_with_audit(self) -> None:
        conn = FakeConn(self._script)
        row = rec.set_application_status(
            conn, user_id="u", company_id="c", role_normalized="sde",
            status=ApplicationStatus.APPLIED, source="telegram:button")
        assert row["status"] == "APPLIED"
        audits = [sql for sql, _ in conn.executed if "audit_logs" in sql]
        assert len(audits) == 1
        assert any("application.status_change" in sql for sql in audits)


class TestListGate:
    def _script_not_found(self, sql: str, params: dict | None):
        if "FROM document_extractions" in sql:
            return FakeResult(mappings_list=[
                {"structured_payload": {"detection": {"is_candidate_list": True}}}])
        if "FROM eligibility_records" in sql:
            return FakeResult(mapping=None)
        return FakeResult()

    def _script_eligible(self, sql: str, params: dict | None):
        if "FROM document_extractions" in sql:
            return FakeResult(mappings_list=[
                {"structured_payload": {"detection": {"is_candidate_list": True}}}])
        if "FROM eligibility_records" in sql:
            return FakeResult(mapping={"1": 1})
        return FakeResult()

    def _script_no_list(self, sql: str, params: dict | None):
        if "FROM document_extractions" in sql:
            return FakeResult(mappings_list=[
                {"structured_payload": {"detection": {"is_candidate_list": False}}}])
        return FakeResult()

    def test_csv_without_name_is_not_found(self) -> None:
        assert rec.message_list_gate(
            FakeConn(self._script_not_found), "msg-1", "comp-1") == "not_found"

    def test_csv_with_name_is_eligible(self) -> None:
        assert rec.message_list_gate(
            FakeConn(self._script_eligible), "msg-1", "comp-1") == "eligible"

    def test_text_only_is_no_list(self) -> None:
        assert rec.message_list_gate(
            FakeConn(self._script_no_list), "msg-1", "comp-1") == "no_list"

    def test_missing_message_is_no_list(self) -> None:
        assert rec.message_list_gate(FakeConn(self._script_no_list), None,
                                     "comp-1") == "no_list"
