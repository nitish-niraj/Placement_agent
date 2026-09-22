"""Inline pre-fill links: entry extraction, conservative mapping, prefill-URL
building, SSRF guard, and the endpoint (DB faked, network stubbed — no
Postgres, no live HTTP)."""

import contextlib

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from pia_api import prefill as prefill_mod
from pia_api.main import create_app
from pia_api.routes import actions as actions_module
from pia_api.settings import Settings

TOKEN = "test-dashboard-token"
ACTION_ID = "00000000-0000-0000-0000-00000000000a"
ACTION_ROW = {
    "id": ACTION_ID, "type": "form_draft",
    "target": "https://docs.google.com/forms/d/e/ABC/viewform",
    "payload": {"form_url": "https://docs.google.com/forms/d/e/ABC/viewform",
                "company": "SOFTLINK"},
    "risk_level": "medium", "status": "WAITING_APPROVAL",
    "approval_required": True, "created_at": "2026-09-13T10:00:00+00:00",
    "updated_at": "2026-09-13T10:00:00+00:00",
    "event_id": None, "event_title": None, "company": "SOFTLINK",
}
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CANONICAL = "https://docs.google.com/forms/d/e/ABC/viewform?usp=send_form"

BLOB = ('FB_PUBLIC_LOAD_DATA_ = [null,['
        '[1838871834,"Registration Number",null,0,[[440445981,null,1]]],'
        '[134514418,"Name",null,0,[[1402618024,null,1]]],'
        '[2108103322,"Registered on Accenture Portal",null,2,'
        '[[488462340,[["Yes",null],["No",null]],1,null,null,0]]],'
        '[7770011,"Faculty \\"Name\\"",null,0,[]],'
        ']];')
HTML = f"<html><head></head><body><script>{BLOB}</script></body></html>"


class TestExtract:
    def test_items_and_options(self) -> None:
        entries = prefill_mod.extract_entries(HTML)
        by_id = {e.entry_id: e for e in entries}
        # Text items answer under the nested response id from their payload.
        assert by_id["440445981"].text == "Registration Number"
        assert by_id["440445981"].kind == "text"
        assert by_id["440445981"].options == ()
        assert by_id["1402618024"].text == "Name"
        # Choice items answer under their nested response sub-id.
        assert by_id["488462340"].text == "Registered on Accenture Portal"
        assert by_id["488462340"].kind == "choice"
        assert by_id["488462340"].options == ("Yes", "No")
        assert by_id["7770011"].text == 'Faculty "Name"'

    def test_no_blob_is_empty_not_error(self) -> None:
        assert prefill_mod.extract_entries("<html>sign in wall</html>") == []

    def test_item_extent_skips_brackets_in_strings(self) -> None:
        blob = ('[1000001,"a [bracket] title",null,2,'
                '[[7654321,[["Yes",null]]]]]')
        entries = prefill_mod.extract_entries("FB_PUBLIC_LOAD_DATA_ = " + blob)
        assert entries[0].options == ("Yes",)


class TestMap:
    def test_hint_match_fills_text(self) -> None:
        entries = prefill_mod.extract_entries(HTML)
        filled, blank = prefill_mod.map_values(
            entries, {"registration_number": "12515641", "full_name": "Nitish"})
        assert filled == {"440445981": "12515641", "1402618024": "Nitish"}
        assert {b["question"] for b in blank} == {
            "Registered on Accenture Portal", 'Faculty "Name"'}

    def test_judgement_question_always_blank(self) -> None:
        # No catalog value could ever answer this — left for the owner.
        (entry,) = [e for e in prefill_mod.extract_entries(HTML)
                    if e.entry_id == "488462340"]
        filled, blank = prefill_mod.map_values([entry], {"company": "Accenture"})
        assert filled == {}
        assert blank == [{"question": "Registered on Accenture Portal",
                          "reason": "no matching profile field"}]

    def test_email_hint_never_fills_yes_no_choice(self) -> None:
        # Regression: "Confirmation email received ...?" contains "email" but
        # asks a Yes/No judgement — the address must not leak into it.
        entry = prefill_mod.FormEntry("933606138", "Confirmation email received?",
                                      "choice", ("Yes", "No"))
        filled, blank = prefill_mod.map_values([entry], {"email": "nitish@lpu.in"})
        assert filled == {}
        assert blank[0]["question"] == "Confirmation email received?"

    def test_exact_option_uses_canonical_text(self) -> None:
        entries = [prefill_mod.FormEntry("1", "Which company", "choice",
                                         ("Accenture", "TCS"))]
        filled, _ = prefill_mod.map_values(entries, {"company": "accenture"})
        assert filled == {"1": "Accenture"}

    def test_mobile_hint_fills_text(self) -> None:
        entries = [prefill_mod.FormEntry("9", "Mobile No", "text", ())]
        filled, blank = prefill_mod.map_values(entries, {"mobile": "98765 43210"})
        assert filled == {"9": "98765 43210"}
        assert blank == []
        _, blank = prefill_mod.map_values(entries, {})
        assert blank == [{"question": "Mobile No", "reason": "no value on file"}]

    def test_missing_value_reported(self) -> None:
        entries = [prefill_mod.FormEntry("1", "Registration Number", "text", ())]
        _, blank = prefill_mod.map_values(entries, {})
        assert blank == [{"question": "Registration Number",
                          "reason": "no value on file"}]

    def test_unknown_kind_always_blank(self) -> None:
        entries = [prefill_mod.FormEntry("1", "Upload resume", "other", ())]
        filled, blank = prefill_mod.map_values(entries, {"full_name": "Nitish"})
        assert filled == {}
        assert blank[0]["reason"].startswith("unsupported question type")


class TestBuildUrl:
    def test_params_and_embedded(self) -> None:
        url = prefill_mod.build_prefill_url(
            CANONICAL, {"440445981": "12515641", "1402618024": "Nitish Kumar"})
        assert "entry.440445981=12515641" in url
        assert "entry.1402618024=Nitish+Kumar" in url
        assert "embedded=true" in url
        assert "usp=pp_url" in url
        assert "usp=send_form" not in url

    def test_stale_entry_params_replaced(self) -> None:
        url = prefill_mod.build_prefill_url(CANONICAL + "&entry.1=old", {"1": "new"})
        assert "entry.1=old" not in url and "entry.1=new" in url

    def test_edit_requested_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            prefill_mod, "resolve_form_url",
            lambda target: CANONICAL + "&edit_requested=true")
        monkeypatch.setattr(prefill_mod, "fetch_form_html", lambda url: "")
        report = prefill_mod.prefill_for_target("https://tinyurl.com/x", {})
        assert "edit_requested" not in report["prefill_url"]
        assert "embedded=true" in report["prefill_url"]


class FakeResp:
    def __init__(self, url: str, status: int = 200, body: str = "") -> None:
        self.url = url
        self.status_code = status
        self.content = body.encode()
        self.text = body


class FakeClient:
    final_url = CANONICAL
    status = 200
    body = HTML

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def get(self, url: str) -> FakeResp:
        return FakeResp(self.final_url, self.status, self.body)


class TestResolve:
    def test_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(prefill_mod.httpx, "Client", FakeClient)
        assert prefill_mod.resolve_form_url("https://forms.gle/ABC") == CANONICAL

    def test_auth_walled_google_url_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Edit links 401 plain-HTTP clients; the fetch stage turns that into
        # the manual-note report instead of an error.
        FakeClient.status = 401
        try:
            monkeypatch.setattr(prefill_mod.httpx, "Client", FakeClient)
            assert prefill_mod.resolve_form_url("https://tinyurl.com/x") == CANONICAL
        finally:
            FakeClient.status = 200

    def test_non_http_rejected(self) -> None:
        with pytest.raises(prefill_mod.PrefillError):
            prefill_mod.resolve_form_url("file:///etc/passwd")

    def test_non_google_final_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        FakeClient.final_url = "https://evil.example/phish"
        try:
            monkeypatch.setattr(prefill_mod.httpx, "Client", FakeClient)
            with pytest.raises(prefill_mod.PrefillError):
                prefill_mod.resolve_form_url("https://tinyurl.com/x")
        finally:
            FakeClient.final_url = CANONICAL

    def test_teams_meeting_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        FakeClient.final_url = ("https://teams.microsoft.com/dl/launcher/"
                                "launcher.html?url=%2Fmeet%2F123")
        try:
            monkeypatch.setattr(prefill_mod.httpx, "Client", FakeClient)
            with pytest.raises(prefill_mod.PrefillError) as excinfo:
                prefill_mod.resolve_form_url("https://tinyurl.com/x")
            assert excinfo.value.status_code == 422
            assert "Teams meeting" in str(excinfo.value)
        finally:
            FakeClient.final_url = CANONICAL

    def test_microsoft_final_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        FakeClient.final_url = "https://forms.office.com/r/AbCdEf123"
        try:
            monkeypatch.setattr(prefill_mod.httpx, "Client", FakeClient)
            assert prefill_mod.resolve_form_url(
                "https://tinyurl.com/x") == FakeClient.final_url
        finally:
            FakeClient.final_url = CANONICAL

    def test_microsoft_gets_manual_report(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ms_url = "https://forms.office.com/r/AbCdEf123"
        monkeypatch.setattr(prefill_mod, "resolve_form_url", lambda target: ms_url)
        calls: list[str] = []
        monkeypatch.setattr(prefill_mod, "fetch_form_html",
                            lambda url: calls.append(url) or "")
        report = prefill_mod.prefill_for_target("https://tinyurl.com/x", {})
        assert report["provider"] == "microsoft"
        assert report["filled"] == [] and "bookmark" in report["note"]
        assert report["prefill_url"] == ms_url
        assert calls == []  # no fetch attempted for manual providers


class TestFetch:
    def test_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(prefill_mod.httpx, "Client", FakeClient)
        assert "FB_PUBLIC_LOAD_DATA_" in prefill_mod.fetch_form_html(CANONICAL)

    def test_auth_wall_is_empty_not_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeClient.status = 401
        try:
            monkeypatch.setattr(prefill_mod.httpx, "Client", FakeClient)
            assert prefill_mod.fetch_form_html(CANONICAL) == ""
        finally:
            FakeClient.status = 200

    def test_server_error_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        FakeClient.status = 500
        try:
            monkeypatch.setattr(prefill_mod.httpx, "Client", FakeClient)
            with pytest.raises(prefill_mod.PrefillError):
                prefill_mod.fetch_form_html(CANONICAL)
        finally:
            FakeClient.status = 200

    def test_walled_target_reports_manual(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(prefill_mod, "resolve_form_url", lambda target: CANONICAL)
        monkeypatch.setattr(prefill_mod, "fetch_form_html", lambda url: "")
        report = prefill_mod.prefill_for_target("https://tinyurl.com/x", {})
        assert report["filled"] == [] and "sign-in" in report["note"]
        assert "embedded=true" in report["prefill_url"]


class FakeResult:
    def __init__(self, mapping=None):
        self._mapping = mapping

    def mappings(self):
        return self

    def first(self):
        return self._mapping


class FakeConn:
    def execute(self, sql, params=None):
        text = str(sql)
        if "FROM actions a" in text:
            return FakeResult(mapping=dict(ACTION_ROW))
        if "FROM candidate_profiles" in text:
            return FakeResult(mapping={
                "canonical_name": "Nitish Kumar", "email": "nitish@lpu.in",
                "roll_number": "12515641", "registration_number": "12515641",
                "student_id": None, "branch": "CSE", "batch": "2023-2027",
                "cgpa": 7.9, "tenth_percent": 88.0, "twelfth_percent": 84.0,
                "backlog_count": 0, "display_name": "Nitish Kumar",
                "mobile_number": None})
        return FakeResult(mapping=None)


class FakeEngine:
    def connect(self):
        return contextlib.nullcontext(FakeConn())

    def begin(self):
        return contextlib.nullcontext(FakeConn())


def _wire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pia_api.settings._settings",
        Settings(_env_file=None, dashboard_token=TOKEN),  # type: ignore[call-arg]
    )
    monkeypatch.setattr(actions_module, "get_engine", lambda: FakeEngine())
    monkeypatch.setattr(prefill_mod, "resolve_form_url", lambda target: CANONICAL)
    monkeypatch.setattr(prefill_mod, "fetch_form_html", lambda url: HTML)


class TestEndpoint:
    def test_prefill_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch)
        response = TestClient(create_app()).get(
            f"/api/v1/actions/{ACTION_ID}/prefill-url", headers=AUTH)
        assert response.status_code == 200, response.text
        body = response.json()
        assert "entry.440445981=12515641" in body["prefill_url"]
        assert "embedded=true" in body["prefill_url"]
        assert any("Registration Number" in f["question"] for f in body["filled"])
        assert any("Registered on Accenture Portal" in b["question"]
                   for b in body["left_blank"])

    def test_unknown_action_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch)

        def missing(conn, action_id: str) -> dict:
            raise HTTPException(status_code=404, detail="action not found")

        monkeypatch.setattr(actions_module, "_load_action", missing)
        response = TestClient(create_app()).get(
            f"/api/v1/actions/{ACTION_ID}/prefill-url", headers=AUTH)
        assert response.status_code == 404

    def test_non_form_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch)
        monkeypatch.setattr(
            actions_module, "_load_action",
            lambda conn, action_id: dict(ACTION_ROW, type="deadline_nudge"))
        response = TestClient(create_app()).get(
            f"/api/v1/actions/{ACTION_ID}/prefill-url", headers=AUTH)
        assert response.status_code == 422

    def test_approve_form_draft_never_enqueues_robot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)

        def boom(action_id: str) -> None:
            raise AssertionError("robot must not be enqueued")

        monkeypatch.setattr("pia_api.jobs.enqueue_form_submit", boom)
        response = TestClient(create_app()).post(
            f"/api/v1/actions/{ACTION_ID}/approve", headers=AUTH)
        # Approval itself still records; what matters: no boom.
        assert response.status_code == 200
        assert response.json()["status"] == "APPROVED"
