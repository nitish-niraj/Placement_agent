"""P13/F-030 propose_form_action: pure flow over a scripted fake connection.

Covers: proposal inserts PROPOSED and walks it to WAITING_APPROVAL (§10.4
machine asserted), idempotency per form URL (non-terminal and decided drafts
block re-proposal; FAILED may be retried), non-proposable types and link-less
events skip. The payload must carry references only — no decrypted PII."""

import contextlib
import json
from types import SimpleNamespace

import pytest

from pia_worker.jobs import propose_actions as pa

FORM_URL = "https://docs.google.com/forms/d/e/ABC123/viewform"


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


def _script(existing_status: str | None, event_type: str = "FORM",
            links: list[str] | None = None, excerpt: str = "",
            designation: str | None = None,
            extraction_payloads: list | None = None,
            app_row: dict | None = "default"):
    links = [FORM_URL] if links is None else links
    payload = {"links": links, "excerpt": excerpt,
               "designation": designation, "source_message_id": "msg-1"}

    def script(sql: str, _params: dict | None = None) -> FakeResult:
        if "FROM events e" in sql:
            return FakeResult(mapping={"type": event_type, "company": "SOFTLINK",
                                       "company_id": "comp-uuid",
                                       "current_payload": dict(payload),
                                       "links": links})
        if "FROM document_extractions" in sql:
            return FakeResult(mapping_list=extraction_payloads or [])
        if "FROM users" in sql:
            return FakeResult(value="user-uuid",
                              mapping=SimpleNamespace(id="user-uuid"))
        if "FROM application_states" in sql:
            if app_row == "default":
                return FakeResult(mapping=None)  # no tracked row (UNKNOWN)
            return FakeResult(mapping=app_row)
        if "FROM actions WHERE target" in sql:
            return FakeResult(value=existing_status)
        if "INSERT INTO actions" in sql:
            return FakeResult(value="action-uuid")
        return FakeResult()  # UPDATE / audit_logs

    return script


class TestProposalFlow:
    def _run(self, monkeypatch: pytest.MonkeyPatch, existing: str | None,
             event_type: str = "FORM", links: list[str] | None = None) -> FakeConn:
        conn = FakeConn(_script(existing, event_type, links))
        monkeypatch.setattr(pa, "_engine_for_current_host",
                            lambda: FakeEngine(conn))
        return conn

    def test_proposes_and_walks_to_waiting_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = self._run(monkeypatch, existing=None)
        assert pa.propose_form_action("event-uuid") == "proposed"
        updates = [sql for sql, _ in conn.executed if "UPDATE actions" in sql]
        assert updates and "WAITING_APPROVAL" in updates[0]
        audits = [p for sql, p in conn.executed if "audit_logs" in sql and p]
        assert {a["action"] for a in audits} == {"action.propose",
                                                 "action.await_approval"}

    def test_payload_holds_references_only_no_pii(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = self._run(monkeypatch, existing=None)
        pa.propose_form_action("event-uuid")
        inserts = [p for sql, p in conn.executed
                   if "INSERT INTO actions" in sql and p]
        payload = json.loads(inserts[0]["payload"])
        assert payload["form_url"] == FORM_URL
        assert payload["event_id"] == "event-uuid"
        assert "prefill" not in payload
        assert not any(key in payload for key in ("roll_number", "email", "cgpa"))

    def test_idempotent_same_url_blocks_reproposal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = self._run(monkeypatch, existing="WAITING_APPROVAL")
        assert pa.propose_form_action("event-uuid") == "already_proposed"
        assert not any("INSERT INTO actions" in sql for sql, _ in conn.executed)

    def test_rejected_decision_stands_no_reproposal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._run(monkeypatch, existing="REJECTED")
        assert pa.propose_form_action("event-uuid") == "already_proposed"

    def test_failed_action_may_be_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._run(monkeypatch, existing="FAILED")
        assert pa.propose_form_action("event-uuid") == "proposed"

    def test_non_proposable_type_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._run(monkeypatch, existing=None, event_type="OA")
        assert pa.propose_form_action("event-uuid") == "skipped_type"

    def test_event_without_link_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._run(monkeypatch, existing=None, links=[])
        assert pa.propose_form_action("event-uuid") == "skipped_no_link"


class TestLinkResolution:
    """Short links hide the destination: resolve to the canonical Google Form
    (dedup then works across shorteners), refuse proven non-forms, and never
    lose a draft to a transient network error."""

    CANONICAL = "https://docs.google.com/forms/d/e/XYZ/viewform"
    TEAMS = "https://teams.microsoft.com/dl/launcher/launcher.html?url=x"

    def test_short_link_canonicalized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = FakeConn(_script(None, links=["https://tinyurl.com/x"]))
        monkeypatch.setattr(pa, "_engine_for_current_host",
                            lambda: FakeEngine(conn))
        monkeypatch.setattr(pa, "_fetch_final_url", lambda url: self.CANONICAL)
        assert pa.propose_form_action("event-uuid") == "proposed"
        inserts = [p for sql, p in conn.executed
                   if "INSERT INTO actions" in sql and p]
        assert inserts[0]["target"] == self.CANONICAL
        assert json.loads(inserts[0]["payload"])["form_url"] == self.CANONICAL

    def test_proven_non_form_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = FakeConn(_script(None, links=["https://tinyurl.com/x"]))
        monkeypatch.setattr(pa, "_engine_for_current_host",
                            lambda: FakeEngine(conn))
        monkeypatch.setattr(pa, "_fetch_final_url", lambda url: self.TEAMS)
        assert pa.propose_form_action("event-uuid") == "skipped_not_a_form"
        assert not any("INSERT INTO actions" in sql for sql, _ in conn.executed)

    def test_unresolvable_link_kept_fail_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(url: str) -> str:
            raise TimeoutError("no network")

        conn = FakeConn(_script(None, links=["https://tinyurl.com/x"]))
        monkeypatch.setattr(pa, "_engine_for_current_host",
                            lambda: FakeEngine(conn))
        monkeypatch.setattr(pa, "_fetch_final_url", boom)
        monkeypatch.setattr(pa.time, "sleep", lambda s: None)
        assert pa.propose_form_action("event-uuid") == "proposed"

    def test_transient_blip_retried_then_canonicalized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 04:14 DNS blip minted a bogus draft: one failure must not
        fall through to fail-open while retries remain."""
        calls = {"n": 0}

        def flaky(url: str) -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("dns blip")
            return TestLinkResolution.CANONICAL

        conn = FakeConn(_script(None, links=["https://tinyurl.com/x"]))
        monkeypatch.setattr(pa, "_engine_for_current_host",
                            lambda: FakeEngine(conn))
        monkeypatch.setattr(pa, "_fetch_final_url", flaky)
        monkeypatch.setattr(pa.time, "sleep", lambda s: None)
        assert pa.propose_form_action("event-uuid") == "proposed"
        inserts = [p for sql, p in conn.executed
                   if "INSERT INTO actions" in sql and p]
        assert inserts[0]["target"] == TestLinkResolution.CANONICAL
        assert calls["n"] == 3

    def test_meeting_non_form_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = FakeConn(_script(None))
        monkeypatch.setattr(pa, "_engine_for_current_host",
                            lambda: FakeEngine(conn))
        monkeypatch.setattr(pa, "_fetch_final_url", lambda url: self.TEAMS)
        assert pa.propose_meeting_form_action("https://tinyurl.com/x") == (
            "skipped_not_a_form")

    def test_microsoft_and_glide_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for final in ("https://forms.office.com/r/AbCdEf123",
                      "https://forms.glide.com/f/abc"):
            self._propose_short(monkeypatch, final)

    def _propose_short(self, monkeypatch: pytest.MonkeyPatch, final: str) -> None:
        conn = FakeConn(_script(None, links=["https://tinyurl.com/x"]))
        monkeypatch.setattr(pa, "_engine_for_current_host",
                            lambda: FakeEngine(conn))
        monkeypatch.setattr(pa, "_fetch_final_url", lambda url: final)
        assert pa.propose_form_action("event-uuid") == "proposed"
        inserts = [p for sql, p in conn.executed
                   if "INSERT INTO actions" in sql and p]
        assert inserts[0]["target"] == final


class TestMeetingProposal:
    """Listener entry (P13): a form link relayed from the Teams meeting chat
    becomes a draft carrying the caption-detected teacher/presenter names."""

    def _run(self, monkeypatch: pytest.MonkeyPatch,
             existing: str | None = None) -> FakeConn:
        conn = FakeConn(_script(existing))
        monkeypatch.setattr(pa, "_engine_for_current_host",
                            lambda: FakeEngine(conn))
        return conn

    def test_proposes_with_presenters_in_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = self._run(monkeypatch)
        outcome = pa.propose_meeting_form_action(
            FORM_URL + "/", presenters=("Dr. Sharma", "Prof. Verma"))
        assert outcome == "proposed"
        inserts = [p for sql, p in conn.executed
                   if "INSERT INTO actions" in sql and p]
        payload = json.loads(inserts[0]["payload"])
        assert payload["source"] == "teams_meeting_chat"
        assert payload["presenters"] == ["Dr. Sharma", "Prof. Verma"]
        assert inserts[0]["target"] == FORM_URL  # trailing slash trimmed

    def test_no_presenters_is_fine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = self._run(monkeypatch)
        assert pa.propose_meeting_form_action(FORM_URL) == "proposed"
        inserts = [p for sql, p in conn.executed
                   if "INSERT INTO actions" in sql and p]
        assert json.loads(inserts[0]["payload"])["presenters"] == []

    def test_blank_url_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = self._run(monkeypatch)
        assert pa.propose_meeting_form_action("") == "skipped_no_link"
        assert not any("INSERT INTO actions" in sql for sql, _ in conn.executed)

    def test_idempotent_per_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._run(monkeypatch, existing="WAITING_APPROVAL")
        assert pa.propose_meeting_form_action(FORM_URL) == "already_proposed"


def _app_row(status: str) -> dict:
    return {"id": "app-uuid", "status": status, "applied_at": None,
            "source": "test", "note": "", "opportunity_key": "softlink|",
            "updated_at": None}


def _list_payload() -> list:
    return [{"structured_payload": {"detection": {"is_candidate_list": True}}}]


class TestApplicationGates:
    """Company mentioned != applied: the strict list gate and the negative
    application answer both skip the draft; undecided/APPLIED still propose."""

    def _run_gate(self, monkeypatch: pytest.MonkeyPatch, **kwargs) -> None:
        conn = FakeConn(_script(None, **kwargs))
        monkeypatch.setattr(pa, "_engine_for_current_host",
                            lambda: FakeEngine(conn))

    def test_csv_without_name_skips_draft(self,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
        self._run_gate(monkeypatch,
                       excerpt="Fill the Mars.AI account creation form.",
                       extraction_payloads=_list_payload())
        assert pa.propose_form_action("event-uuid") == "skipped_not_in_list"

    def test_not_applied_skips_post_application_draft(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._run_gate(
            monkeypatch,
            excerpt="Candidates who applied must complete verification.",
            app_row=_app_row("NOT_APPLIED"))
        assert pa.propose_form_action("event-uuid") == "skipped_not_applied"

    def test_not_interested_skips_draft(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._run_gate(monkeypatch, excerpt="",
                       app_row=_app_row("NOT_INTERESTED"))
        assert pa.propose_form_action("event-uuid") == "skipped_not_applied"

    def test_opening_shaped_message_still_proposes(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._run_gate(monkeypatch,
                       excerpt="Mars.AI has opened applications for freshers.",
                       app_row=_app_row("NOT_APPLIED"))
        assert pa.propose_form_action("event-uuid") == "proposed"

    def test_undecided_still_proposes(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._run_gate(
            monkeypatch,
            excerpt="Candidates who applied must complete verification.",
            app_row=_app_row("UNKNOWN"))
        assert pa.propose_form_action("event-uuid") == "proposed"

    def test_applied_proposes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._run_gate(
            monkeypatch,
            excerpt="Candidates who applied must complete verification.",
            app_row=_app_row("APPLIED"))
        assert pa.propose_form_action("event-uuid") == "proposed"
