"""P10 capstone — golden E2E scenario (master §20.2 / 03_App_Flow F10).

Runs against the live compose stack (marked integration). Telegram delivery is
stubbed so the test asserts BEHAVIOR, not chat noise:

    steps 6-7:  eligible company + new event  -> exactly ONE HIGH alert
    step 12-13: the same announcement repeats -> suppressed by dedup
    step 14-15: the time changes (delta)      -> exactly ONE update alert
"""

import datetime as dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy

from pia_shared.enums import EventType
from pia_worker.companies.resolver import resolve_company
from pia_worker.events.canonical import EventPlan
from pia_worker.events.records import upsert_event
from pia_worker.jobs import notify as notify_jobs
from pia_worker.jobs.process_message import _engine
from pia_worker.notify.channel import DeliveryResult

pytestmark = pytest.mark.integration

# The integration run talks to the LIVE stack: credentials come from
# infrastructure/.env (never committed) when not already in the environment.
_env_file = Path(__file__).parents[2] / "infrastructure" / ".env"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value and key not in os.environ and not key.startswith("#"):
            os.environ[key] = value
    if "DATABASE_URL" not in os.environ:
        os.environ["DATABASE_URL"] = (
            f"postgresql+psycopg://{os.environ.get('POSTGRES_USER', 'pia')}:"
            f"{os.environ.get('POSTGRES_PASSWORD', 'pia')}@localhost:5432/"
            f"{os.environ.get('POSTGRES_DB', 'pia')}"
        )

IST = ZoneInfo("Asia/Kolkata")


class StubChannel:
    sent: list[str] = []

    def send(self, chat_id: str, text: str) -> DeliveryResult:
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


def _plan(company_id: str | None, company_key: str | None,
          deadline: dt.datetime, links: list[str]) -> EventPlan:
    return EventPlan(
        event_type=EventType.OA, company_id=company_id, company_key=company_key,
        action="attend", deadline_at=deadline, links=links,
        excerpt="Golden scenario OA announcement", source_message_id=None,
    )


def test_golden_scenario_eligible_company_alert_dedup_and_delta(
    stub_delivery: list[str],
) -> None:
    engine = _engine()
    now = dt.datetime.now(tz=IST)
    unique = f"golden scenario test co {int(now.timestamp())}"
    with engine.begin() as conn:
        company_id = resolve_company(conn, unique).company_id
        event_id, outcome = upsert_event(
            conn,
            _plan(company_id, unique, now + dt.timedelta(days=2),
                  ["https://example.com/golden-oa"]),
            message_id=None, group_id=None,
        )
    assert outcome == "created"

    # step 6-7: relevance engine sees the event; one HIGH alert goes out
    first = notify_jobs.notify_event(event_id, "created")
    assert first == "queued:HIGH"
    assert len(stub_delivery) == 1
    assert "golden scenario test co" in stub_delivery[0]

    # steps 12-13: the same announcement repeats three times -> suppressed
    for _ in range(3):
        assert notify_jobs.notify_event(event_id, "created") == "suppressed_dedup"
    assert len(stub_delivery) == 1  # no repeat spam (FR-NOT-004)

    # steps 14-15: the OA time changes -> ONE delta update notification
    with engine.begin() as conn:
        delta_event_id, delta_outcome = upsert_event(
            conn,
            _plan(company_id, "golden scenario test co",
                  now + dt.timedelta(days=2, hours=1),
                  ["https://example.com/golden-oa", "https://example.com/new-link"]),
            message_id=None, group_id=None,
        )
    assert delta_outcome == "delta"
    assert delta_event_id == event_id
    assert notify_jobs.notify_event(event_id, "delta") == "queued:HIGH"
    assert len(stub_delivery) == 2  # exactly one delta alert, never a re-send

    # delivery history records both sends (FR-NOT-007 auditability)
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT d.status FROM notification_deliveries d "
                "JOIN notifications n ON n.id = d.notification_id "
                "WHERE n.event_id = CAST(:eid AS uuid)"
            ),
            {"eid": event_id},
        ).all()
    assert len(rows) == 2
    assert all(row.status == "sent" for row in rows)

    # cleanup: the scenario artifact never lingers in live data
    with engine.begin() as conn:
        conn.execute(
            sqlalchemy.text(
                "DELETE FROM notification_deliveries WHERE notification_id IN "
                "(SELECT id FROM notifications WHERE event_id = CAST(:eid AS uuid))"
            ),
            {"eid": event_id},
        )
        conn.execute(
            sqlalchemy.text(
                "DELETE FROM notifications WHERE event_id = CAST(:eid AS uuid)"
            ),
            {"eid": event_id},
        )
        conn.execute(
            sqlalchemy.text(
                "DELETE FROM event_updates WHERE event_id = CAST(:eid AS uuid)"
            ),
            {"eid": event_id},
        )
        conn.execute(
            sqlalchemy.text("DELETE FROM events WHERE id = CAST(:eid AS uuid)"),
            {"eid": event_id},
        )
        conn.execute(
            sqlalchemy.text("DELETE FROM company_aliases WHERE company_id = CAST(:cid AS uuid)"),
            {"cid": company_id},
        )
        conn.execute(
            sqlalchemy.text("DELETE FROM companies WHERE id = CAST(:cid AS uuid)"),
            {"cid": company_id},
        )
