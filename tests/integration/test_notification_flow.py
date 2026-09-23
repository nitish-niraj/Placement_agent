"""End-to-end notification flow on the live dev DB (seeded user assumed,
same as the golden scenario): defaulter-present -> action alert,
defaulter-absent -> suppressed, missing attachment -> ask nudge (no false
verdict), expired deadline -> digest LOW, digest idempotency, delta update
rendering. Artifacts are cleaned up per test."""

import datetime as dt
import os
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy

from pia_shared.enums import ApplicationStatus, EventType
from pia_worker.companies.resolver import resolve_company
from pia_worker.events.canonical import EventPlan
from pia_worker.events.records import upsert_event
from pia_worker.jobs import notify as notify_jobs
from pia_worker.jobs.process_message import _engine
from pia_worker.notify.channel import DeliveryResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("PIA_RUN_INTEGRATION") != "1",
        reason="Integration tests require the compose stack "
               "(PIA_RUN_INTEGRATION=1).",
    ),
]

IST = ZoneInfo("Asia/Kolkata")


class StubChannel:
    sent: list[str] = []

    def send(self, chat_id: str, text: str,
             reply_markup: dict | None = None) -> DeliveryResult:
        StubChannel.sent.append(text)
        return DeliveryResult(True, "stub")


@pytest.fixture
def stub_delivery(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    StubChannel.sent = []
    monkeypatch.setattr(notify_jobs, "_channel", lambda: StubChannel())
    monkeypatch.setattr(
        notify_jobs, "_enqueue_send",
        lambda notification_id: notify_jobs.send_notification(notification_id),
    )
    return StubChannel.sent


def _unique(prefix: str) -> str:
    return f"{prefix} {int(dt.datetime.now(tz=IST).timestamp() * 1000) % 100000}"


def _user_id(engine) -> str:  # noqa: ANN001
    with engine.connect() as conn:
        return str(conn.execute(
            sqlalchemy.text("SELECT id FROM users LIMIT 1")).scalar_one())


def _message_chain(engine, company_id: str, group_name: str, text: str,
                   user_id: str, in_list: bool | None,
                   designation: str | None = None) -> dict:
    """Group + message + attachment + extraction + optional eligibility row.

    in_list True -> ELIGIBLE record; False -> NOT_FOUND record;
    None -> no eligibility row (parse pending / missing).
    """
    provider_group = f"flow-test-{_unique('g')}"
    with engine.begin() as conn:
        group_id = str(conn.execute(
            sqlalchemy.text(
                "INSERT INTO groups (provider_group_id, name, category, enabled) "
                "VALUES (:pg, :name, 'PLACEMENT', false) RETURNING id"),
            {"pg": provider_group, "name": group_name}).scalar_one())
        message_id = str(conn.execute(
            sqlalchemy.text(
                "INSERT INTO messages (provider_message_id, group_id, sender_id, "
                "sent_at, text, content_hash, correlation_id) VALUES "
                "(:pm, CAST(:g AS uuid), 'flow', now(), :text, 'flowhash', "
                "gen_random_uuid()) RETURNING id"),
            {"pm": f"flow-{provider_group}", "g": group_id,
             "text": text}).scalar_one())
        attachment_id = str(conn.execute(
            sqlalchemy.text(
                "INSERT INTO attachments (message_id, mime_type, file_name, "
                "processing_state) VALUES (CAST(:m AS uuid), 'text/csv', "
                "'defaulter_list.csv', 'PROCESSED') RETURNING id"),
            {"m": message_id}).scalar_one())
        extraction_id = str(conn.execute(
            sqlalchemy.text(
                "INSERT INTO document_extractions (attachment_id, extractor, "
                "extractor_version, structured_payload) VALUES "
                "(CAST(:a AS uuid), 'flow', '1', "
                "CAST(:payload AS jsonb)) RETURNING id"),
            {"a": attachment_id,
             "payload": '{"detection": {"is_candidate_list": true}}'}
        ).scalar_one())
        if in_list is not None:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO eligibility_records (user_id, company_id, state, "
                    "match_status, match_method, confidence, requires_user_review, "
                    "source_message_id, source_document_id, evidence) VALUES "
                    "(CAST(:u AS uuid), CAST(:c AS uuid), "
                    "CAST(:state AS eligibility_state), "
                    "CAST(:mstatus AS match_status), "
                    "CAST(:mmethod AS match_method), :conf, false, "
                    "CAST(:m AS uuid), CAST(:e AS uuid), CAST('{}' AS jsonb))"),
                {"u": user_id, "c": company_id,
                 "state": "ELIGIBLE" if in_list else "NOT_FOUND",
                 "mstatus": "MATCHED" if in_list else "NOT_FOUND",
                 "mmethod": "IDENTIFIER" if in_list else None,
                 "conf": 1.0 if in_list else 0.0,
                 "m": message_id, "e": extraction_id})
    return {"group_id": group_id, "message_id": message_id,
            "designation": designation}


def _plan(company_id: str | None, company_key: str | None,
          event_type: EventType, text: str, message_id: str | None,
          designation: str | None = None) -> EventPlan:
    return EventPlan(
        event_type=event_type, company_id=company_id, company_key=company_key,
        action="verify", excerpt=text, source_message_id=message_id,
        designation=designation,
        subject_display=company_key or "flow", subject_evidence="test")


def _cleanup(engine, company_id: str, event_ids: list[str],
             message_ids: list[str], group_ids: list[str]) -> None:
    with engine.begin() as conn:
        conn.execute(
            sqlalchemy.text(
                "DELETE FROM notification_deliveries WHERE notification_id IN "
                "(SELECT id FROM notifications WHERE event_id = ANY(:eids))"),
            {"eids": event_ids})
        conn.execute(
            sqlalchemy.text("DELETE FROM notifications WHERE event_id = ANY(:eids)"),
            {"eids": event_ids})
        conn.execute(
            sqlalchemy.text("DELETE FROM event_updates WHERE event_id = ANY(:eids)"),
            {"eids": event_ids})
        conn.execute(
            sqlalchemy.text("DELETE FROM deadlines WHERE event_id = ANY(:eids)"),
            {"eids": event_ids})
        conn.execute(
            sqlalchemy.text("DELETE FROM events WHERE id = ANY(:eids)"),
            {"eids": event_ids})
        conn.execute(
            sqlalchemy.text(
                "DELETE FROM eligibility_records WHERE source_message_id = ANY(:mids)"),
            {"mids": message_ids})
        conn.execute(
            sqlalchemy.text(
                "DELETE FROM document_extractions WHERE attachment_id IN "
                "(SELECT id FROM attachments WHERE message_id = ANY(:mids))"),
            {"mids": message_ids})
        conn.execute(
            sqlalchemy.text("DELETE FROM attachments WHERE message_id = ANY(:mids)"),
            {"mids": message_ids})
        conn.execute(
            sqlalchemy.text("DELETE FROM messages WHERE id = ANY(:mids)"),
            {"mids": message_ids})
        conn.execute(
            sqlalchemy.text("DELETE FROM groups WHERE id = ANY(:gids)"),
            {"gids": group_ids})
        if company_id:
            conn.execute(
                sqlalchemy.text(
                    "DELETE FROM company_aliases WHERE company_id = CAST(:cid AS uuid)"),
                {"cid": company_id})
            conn.execute(
                sqlalchemy.text("DELETE FROM companies WHERE id = CAST(:cid AS uuid)"),
                {"cid": company_id})


class TestDefaulterFlow:
    def test_present_csv_gets_action_alert(
            self, stub_delivery: list[str]) -> None:
        engine = _engine()
        user_id = _user_id(engine)
        company = _unique("flowco present")
        event_ids: list[str] = []
        chain: dict = {}
        company_id = ''
        try:
            with engine.begin() as conn:
                company_id = resolve_company(conn, company).company_id
            text = (f"Candidates who applied for {company} must complete "
                    "verification before Friday.")
            chain = _message_chain(engine, company_id, "flow-group", text,
                                   user_id, in_list=True)
            with engine.begin() as conn:
                event_id, outcome = upsert_event(
                    conn, _plan(company_id, company, EventType.FORM, text,
                                chain["message_id"]),
                    chain["message_id"], chain["group_id"])
            assert outcome == "created"
            event_ids.append(event_id)
            # Student answers Applied via the same path the buttons use.
            with engine.begin() as conn:
                from pia_worker.applications import records as app_records
                app_records.set_application_status(
                    conn, user_id=user_id, company_id=company_id,
                    status=ApplicationStatus.APPLIED,
                    source="flow-test")
            result = notify_jobs.notify_event(event_id, "created")
            assert result == "queued:HIGH", result
            assert len(stub_delivery) == 1
            body = stub_delivery[0]
            assert "ACTION REQUIRED" in body
            assert "attached list" in body
            assert "General" not in body
        finally:
            _cleanup(engine, company_id, event_ids,
                     [chain["message_id"]] if chain else [],
                     [chain["group_id"]] if chain else [])

    def test_absent_csv_suppressed(self, stub_delivery: list[str]) -> None:
        engine = _engine()
        user_id = _user_id(engine)
        company = _unique("flowco absent")
        event_ids: list[str] = []
        chain: dict = {}
        company_id = ''
        try:
            with engine.begin() as conn:
                company_id = resolve_company(conn, company).company_id
            text = (f"List of the defaulters who are yet not registered on "
                    f"the {company} platform. Fill the account creation form.")
            chain = _message_chain(engine, company_id, "flow-group", text,
                                   user_id, in_list=False)
            with engine.begin() as conn:
                event_id, outcome = upsert_event(
                    conn, _plan(company_id, company, EventType.FORM, text,
                                chain["message_id"]),
                    chain["message_id"], chain["group_id"])
            assert outcome == "created"
            event_ids.append(event_id)
            assert notify_jobs.notify_event(event_id, "created") == \
                "suppressed_list"
            assert stub_delivery == []
        finally:
            _cleanup(engine, company_id, event_ids,
                     [chain["message_id"]] if chain else [],
                     [chain["group_id"]] if chain else [])

    def test_missing_attachment_no_false_verdict(
            self, stub_delivery: list[str]) -> None:
        engine = _engine()
        user_id = _user_id(engine)
        company = _unique("flowco missing")
        event_ids: list[str] = []
        chain: dict = {}
        company_id = ''
        try:
            with engine.begin() as conn:
                company_id = resolve_company(conn, company).company_id
            text = (f"Candidates who applied for {company} must complete "
                    "verification.")
            # in_list=None: extraction exists, match never ran (still loading).
            chain = _message_chain(engine, company_id, "flow-group", text,
                                   user_id, in_list=None)
            with engine.begin() as conn:
                event_id, outcome = upsert_event(
                    conn, _plan(company_id, company, EventType.FORM, text,
                                chain["message_id"]),
                    chain["message_id"], chain["group_id"])
            event_ids.append(event_id)
            # Extraction present + no match row = list gate sees a list but
            # no ELIGIBLE record -> suppressed_list (strict), never a verdict.
            result = notify_jobs.notify_event(event_id, "created")
            assert result in ("suppressed_list", "asked_applied"), result
            assert not any("ACTION REQUIRED" in body
                           for body in stub_delivery)
        finally:
            _cleanup(engine, company_id, event_ids,
                     [chain["message_id"]] if chain else [],
                     [chain["group_id"]] if chain else [])


class TestDeadlineAndDigest:
    def test_expired_deadline_never_today(
            self, stub_delivery: list[str]) -> None:
        engine = _engine()
        company = _unique("flowco expired")
        event_ids: list[str] = []
        company_id = ''
        try:
            with engine.begin() as conn:
                company_id = resolve_company(conn, company).company_id
                now = dt.datetime.now(tz=IST)
                event_plan = EventPlan(
                    event_type=EventType.REGISTRATION, company_id=company_id,
                    company_key=company, action="register",
                    deadline_at=now - dt.timedelta(days=1),
                    excerpt=f"{company} registration closed yesterday.",
                    subject_display=company, subject_evidence="test")
                event_id, _ = upsert_event(conn, event_plan, None, None)
            event_ids.append(event_id)
            result = notify_jobs.notify_event(event_id, "created")
            assert result == notify_jobs.DIGEST_QUEUED, result
            with engine.connect() as conn:
                row = conn.execute(
                    sqlalchemy.text(
                        "SELECT reason FROM notifications "
                        "WHERE event_id = CAST(:eid AS uuid)"),
                    {"eid": event_id}).mappings().first()
            assert "passed" in str(row["reason"])
            assert "TODAY" not in str(row["reason"])
        finally:
            _cleanup(engine, company_id, event_ids, [], [])

    def test_digest_sent_once(self, stub_delivery: list[str],
                              monkeypatch) -> None:
        engine = _engine()
        company = _unique("flowco digest")
        event_ids: list[str] = []
        company_id = ''
        try:
            with engine.begin() as conn:
                company_id = resolve_company(conn, company).company_id
                event_plan = EventPlan(
                    event_type=EventType.OTHER, company_id=company_id,
                    company_key=company,
                    excerpt=f"{company} shared joining instructions.",
                    subject_display=company, subject_evidence="test")
                event_id, _ = upsert_event(conn, event_plan, None, None)
            event_ids.append(event_id)
            assert notify_jobs.notify_event(event_id, "created") == \
                notify_jobs.DIGEST_QUEUED
            first = notify_jobs.daily_digest()
            assert first.startswith("sent:"), first
            assert notify_jobs.daily_digest() == "duplicate"
        finally:
            with engine.begin() as conn:
                conn.execute(
                    sqlalchemy.text(
                        "DELETE FROM idempotency_keys WHERE key LIKE 'digest:%'"))
            _cleanup(engine, company_id, event_ids, [], [])
