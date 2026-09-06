"""P8 job: canonical event + deadline extraction from processed messages
(F-019/F-020/F-021).

Chained from process_message for allowlisted, non-noise messages. Fully
deterministic: rules pick the event type, the date parser resolves times in
Asia/Kolkata, and a missing date means NO deadline (FR-EVT-005) — the flaky
LLM tier cannot corrupt any of this. Idempotent per message via
idempotency_keys.
"""

import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

import sqlalchemy
import structlog

from pia_shared.textnorm import normalize_name
from pia_worker.companies.resolver import resolve_company
from pia_worker.events import rules
from pia_worker.events.dateparse import deadline_from_hit, parse_date_mentions
from pia_worker.events.records import EventPlan, sweep_deadlines, upsert_deadline, upsert_event
from pia_worker.jobs.process_message import PermanentJobError, _engine
from pia_worker.settings import get_settings

logger = structlog.get_logger()


def extract_events(message_id: str) -> str:
    settings = get_settings()
    engine = _engine()

    with engine.connect() as conn:
        message = conn.execute(
            sqlalchemy.text(
                "SELECT m.text, m.received_at, m.group_id, g.enabled AS group_enabled, "
                "m.classification FROM messages m "
                "JOIN groups g ON g.id = m.group_id WHERE m.id = CAST(:id AS uuid)"
            ),
            {"id": message_id},
        ).mappings().first()
        if message is None:
            raise PermanentJobError(f"message {message_id} does not exist")
        if not message["group_enabled"]:  # SEC-006 double-check
            return "skipped_not_allowlisted"
        already = conn.execute(
            sqlalchemy.text(
                "SELECT 1 FROM idempotency_keys WHERE key = :key"
            ),
            {"key": f"events:{message_id}"},
        ).first()
    if already is not None:
        return "already"

    text = message["text"] or ""
    event_type = rules.detect_event_type(text)
    if event_type is None:
        return "no_event"

    # --- company: prefer the resolved ids from P7 mention linkage, fall back
    # to the deterministic drive-code signal in the text itself.
    classification = message["classification"] or {}
    resolved = classification.get("companies_resolved") or []
    company_id = str(resolved[0]["id"]) if resolved else None
    company_key: str | None = None
    company_name: str | None = None
    if resolved:
        company_name = str(resolved[0]["canonical"])
        company_key = normalize_name(company_name)
    else:
        text_company = rules.extract_company_from_text(text)
        if text_company:
            with engine.begin() as conn:
                ref = resolve_company(conn, text_company)
            if ref is not None:  # resolve_company returns None only for blank input
                company_id, company_name = ref.company_id, ref.canonical_name
                company_key = ref.normalized_key

    # --- dates (FR-EVT-002/005): deadline context wins; occurrence dates land
    # in start_at. No parseable date -> both stay None. Never invented.
    received_at = message["received_at"]
    hits = parse_date_mentions(text, received_at)
    deadline_at = None
    start_at = None
    date_phrase = None
    if hits:
        if rules.detect_deadline_context(text):
            deadline_at = deadline_from_hit(hits[0])
            date_phrase = hits[0].phrase
            if len(hits) > 1 and rules.detect_occurrence_context(text):
                start_at = hits[1].resolved
        elif rules.detect_occurrence_context(text):
            start_at = hits[0].resolved
            date_phrase = hits[0].phrase

    entities = classification.get("entities") or {}
    actions = entities.get("actions") or []
    action = (actions[0].get("description") or actions[0].get("type")) \
        if actions else None

    drive_fields = rules.extract_drive_fields(text)

    plan = EventPlan(
        event_type=event_type,
        company_id=company_id,
        company_key=company_key,
        title=f"{company_name or 'General'} — "
              f"{event_type.value.replace('_', ' ').title()}",
        action=action,
        deadline_at=deadline_at,
        start_at=start_at,
        links=rules.extract_links(text),
        venue=rules.extract_venue(text),
        date_phrase=date_phrase,
        excerpt=text,
        source_message_id=message_id,
        designation=drive_fields.get("designation"),
        salary_package=drive_fields.get("salary_package"),
        job_location=drive_fields.get("job_location"),
        eligibility_note=drive_fields.get("eligibility_note"),
    )

    with engine.begin() as conn:
        event_id, outcome = upsert_event(conn, plan, message_id,
                                         str(message["group_id"]))
        if outcome != "duplicate" and plan.deadline_at:
            upsert_deadline(conn, event_id, plan.deadline_at)
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO idempotency_keys (key, fingerprint, entity_ref) "
                "VALUES (:key, :fp, :ref) ON CONFLICT (key) DO NOTHING"
            ),
            {"key": f"events:{message_id}",
             "fp": hashlib.sha256(text.encode()).hexdigest(),
             "ref": event_id},
        )

    # Chain the P10 notification decision (created/delta -> immediate or digest)
    if outcome in ("created", "delta"):
        try:
            from redis import Redis
            from rq import Queue

            from pia_worker.queue import DEFAULT_QUEUE

            Queue(DEFAULT_QUEUE, connection=Redis.from_url(settings.redis_url)).enqueue(
                "pia_worker.jobs.notify.notify_event", event_id, outcome
            )
        except Exception as exc:  # noqa: BLE001 — chaining failure logged, retryable
            logger.warning("notify_enqueue_failed", event_id=event_id,
                           error=str(exc)[:120])

    logger.info("event_extracted", message_id=message_id, event_id=event_id,
                outcome=outcome, type=event_type.value, company=company_name,
                deadline=plan.deadline_at.isoformat() if plan.deadline_at else None,
                start=plan.start_at.isoformat() if plan.start_at else None)
    if outcome == "duplicate":
        _increment_suppressed()
    return f"event:{outcome}"


def _increment_suppressed() -> None:
    """duplicate_suppressed_total{layer="semantic"} — best-effort (TRD §10.2)."""
    try:
        import redis as redis_lib

        from pia_shared.metrics import increment

        client = redis_lib.Redis.from_url(
            get_settings().redis_url, socket_connect_timeout=1
        )
        increment(client, "duplicate_suppressed_total", layer="semantic")
    except Exception as exc:  # noqa: BLE001 — metrics are best-effort
        logger.warning("dedup_metric_failed", error=str(exc)[:120])


def deadline_state_sweep() -> dict[str, int]:
    """F-020 state sweep + FR-NOT-005 escalation reminders (hourly)."""
    settings = get_settings()
    now = datetime.now(tz=ZoneInfo(settings.app_timezone))
    engine = _engine()
    with engine.begin() as conn:
        counts = sweep_deadlines(conn, now)
    if any(counts.values()):
        logger.info("deadline_sweep", **counts)
    try:
        from pia_worker.jobs.notify import deadline_escalations

        counts["reminders_sent"] = deadline_escalations()["reminders_sent"]
    except Exception as exc:  # noqa: BLE001 — reminders never break the sweep
        logger.warning("escalation_pass_failed", error=str(exc)[:120])
    return counts
