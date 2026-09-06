"""P10 jobs: notification decision + delivery + digest (F-023/F-024/F-025).

- `notify_event(event_id, outcome)` — decide priority (§11), dedup via the
  UNIQUE(dedup_key) anchor, then deliver immediately (CRITICAL/HIGH) or queue
  for the digest (MEDIUM/LOW).
- `send_notification(notification_id)` — RQ-retried delivery; exhausted
  retries land in PENDING_DELIVERY (backup path, never silent).
- `notify_eligibility(record_id)` — golden-scenario step 6: one eligibility
  alert with evidence.
- `deadline_escalations()` — FR-NOT-005 reminders per crossed window, wired
  after the P8 deadline sweep.
- `daily_digest()` — F-025: MEDIUM/LOW items composed from canonical events
  only; empty digest is suppressed.

Provider failure never fails the pipeline: Telegram errors become RQ retries,
then PENDING_DELIVERY (TRD §8 backup path).
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import sqlalchemy
import structlog
from rq import get_current_job

from pia_shared.enums import (
    EventType,
    NotificationPriority,
    NotificationStatus,
)
from pia_shared.metrics import increment
from pia_worker.jobs.process_message import _engine
from pia_worker.notify import records as notify_records
from pia_worker.notify.channel import DeliveryError, TelegramChannel
from pia_worker.notify.decide import (
    EventContext,
    dedup_key,
    material_state_hash,
    priority_for_event,
    reminder_windows,
    windows_crossed,
)
from pia_worker.notify.render import (
    render_digest,
    render_eligibility_alert,
    render_event_alert,
    render_reminder,
)
from pia_worker.settings import get_settings

logger = structlog.get_logger()

DIGEST_QUEUED = "queued_for_digest"


def _channel() -> TelegramChannel:
    settings = get_settings()
    return TelegramChannel(settings.telegram_bot_token)


def _load_event(conn: sqlalchemy.Connection, event_id: str) -> dict | None:
    row = conn.execute(
        sqlalchemy.text(
            "SELECT e.id, e.type, e.deadline_at, e.start_at, e.company_id, "
            "e.current_payload, e.group_id, c.canonical_name AS company, "
            "c.watch_state, g.name AS group_name FROM events e "
            "LEFT JOIN companies c ON c.id = e.company_id "
            "LEFT JOIN groups g ON g.id = e.group_id "
            "WHERE e.id = CAST(:id AS uuid)"
        ),
        {"id": event_id},
    ).mappings().first()
    return dict(row) if row else None


def _company_eligible(conn: sqlalchemy.Connection, company_id: str | None) -> bool:
    if not company_id:
        return False
    row = conn.execute(
        sqlalchemy.text(
            "SELECT 1 FROM eligibility_records WHERE company_id = CAST(:c AS uuid) "
            "AND state IN ('ELIGIBLE', 'USER_CONFIRMED') LIMIT 1"
        ),
        {"c": company_id},
    ).first()
    return row is not None


def _enqueue_send(notification_id: str) -> None:
    from redis import Redis
    from rq import Queue, Retry

    from pia_worker.queue import DEFAULT_QUEUE
    from pia_worker.settings import get_settings

    settings = get_settings()
    Queue(DEFAULT_QUEUE, connection=Redis.from_url(settings.redis_url)).enqueue(
        "pia_worker.jobs.notify.send_notification", notification_id,
        retry=Retry(max=settings.notify_send_attempts),
    )


def notify_event(event_id: str, outcome: str = "created") -> str:
    engine = _engine()
    with engine.connect() as conn:
        event = _load_event(conn, event_id)
        if event is None:
            logger.warning("notify_event_missing", event_id=event_id)
            return "missing"
        eligible = _company_eligible(conn, event["company_id"] and str(event["company_id"]))

    payload = event["current_payload"] or {}
    ctx = EventContext(
        event_id=str(event["id"]),
        event_type=EventType(event["type"]),
        deadline_at=event["deadline_at"],
        start_at=event["start_at"],
        has_company=event["company_id"] is not None,
        company_eligible=eligible,
        company_watching=(event["watch_state"] == "WATCHING"),
    )
    decision = priority_for_event(ctx)
    state_hash = material_state_hash(
        event["deadline_at"], event["start_at"], payload.get("venue"),
        payload.get("links") or [], payload.get("designation"),
        payload.get("salary_package"),
    )
    key = dedup_key(str(event["id"]), state_hash)

    if decision.delivery == "digest":
        with engine.begin() as conn:
            notification_id, is_new = notify_records.insert_notification(
                conn, dedup_key=key, priority=decision.priority,
                reason=decision.reason, payload={"outcome": outcome},
                evidence_refs=[], event_id=str(event["id"]),
                message_id=payload.get("source_message_id"),
                status=NotificationStatus.PENDING,  # awaiting the digest job
            )
        if not is_new:
            logger.info("notification_suppressed_dedup", event_id=event_id,
                        dedup_key=key[:12])
            return "suppressed_dedup"
        logger.info("notification_queued_for_digest", event_id=event_id,
                    priority=decision.priority.value)
        return DIGEST_QUEUED

    # CRITICAL/HIGH: render now, persist, deliver via the retried job.
    alert = render_event_alert(
        priority=decision.priority, company=event["company"],
        event_type=EventType(event["type"]).value,
        what_changed=("Event details updated (delta)" if outcome == "delta"
                      else "New announcement for this event"),
        why_me=decision.reason,
        deadline_at=event["deadline_at"], start_at=event["start_at"],
        venue=payload.get("venue"), source_group=event["group_name"],
        designation=payload.get("designation"),
        salary_package=payload.get("salary_package"),
        job_location=payload.get("job_location"),
        eligibility_note=payload.get("eligibility_note"),
        source_excerpt=payload.get("excerpt"),
        source_message_id=payload.get("source_message_id"),
        event_id=str(event["id"]),
    )
    with engine.begin() as conn:
        notification_id, is_new = notify_records.insert_notification(
            conn, dedup_key=key, priority=decision.priority,
            reason=decision.reason,
            payload={"text": alert.text, "outcome": outcome},
            evidence_refs=alert.evidence_refs, event_id=str(event["id"]),
            message_id=payload.get("source_message_id"),
        )
    if not is_new:
        logger.info("notification_suppressed_dedup", event_id=event_id,
                    dedup_key=key[:12])
        return "suppressed_dedup"
    _enqueue_send(notification_id)
    logger.info("notification_queued_immediate", event_id=event_id,
                priority=decision.priority.value, reason=decision.reason)
    return f"queued:{decision.priority.value}"


def send_notification(notification_id: str) -> str:
    settings = get_settings()
    engine = _engine()
    with engine.connect() as conn:
        row = notify_records.load_notification(conn, notification_id)
    if row is None:
        return "missing"
    if NotificationStatus(row["status"]) is NotificationStatus.SENT:
        return "already"  # idempotent re-run after retry

    try:
        result = _channel().send(settings.telegram_chat_id, row["payload"]["text"])
    except DeliveryError as exc:
        job = get_current_job()
        retries_left = getattr(job, "retries_left", 0) if job else 0
        with engine.begin() as conn:
            notify_records.record_delivery(conn, notification_id, "telegram",
                                           ok=False, error=str(exc))
            if retries_left and retries_left > 0:
                logger.warning("telegram_send_retry", notification_id=notification_id,
                               retries_left=retries_left, error=str(exc)[:120])
                raise
            notify_records.mark_pending_delivery(conn, notification_id, str(exc))
        return "pending_delivery"

    with engine.begin() as conn:
        notify_records.mark_sent(conn, notification_id)
        notify_records.record_delivery(conn, notification_id,
                                       result.channel, ok=True, error=None)
    increment(_redis(), "notifications_sent_total",
              priority=str(row["priority"]), channel="telegram")
    logger.info("notification_sent", notification_id=notification_id)
    return "sent"


def _redis():
    from redis import Redis

    from pia_worker.settings import get_settings

    return Redis.from_url(get_settings().redis_url, socket_connect_timeout=1)


def notify_eligibility(record_id: str) -> str:
    """Golden-scenario step 6: one eligibility alert with evidence (FR-NOT-007)."""
    engine = _engine()
    with engine.connect() as conn:
        record = conn.execute(
            sqlalchemy.text(
                "SELECT r.id, r.match_method, r.confidence, r.state, r.evidence, "
                "c.canonical_name AS company, g.name AS group_name "
                "FROM eligibility_records r JOIN companies c ON c.id = r.company_id "
                "LEFT JOIN document_extractions d ON d.id = r.source_document_id "
                "LEFT JOIN attachments a ON a.id = d.attachment_id "
                "LEFT JOIN messages m ON m.id = a.message_id "
                "LEFT JOIN groups g ON g.id = m.group_id "
                "WHERE r.id = CAST(:id AS uuid)"
            ),
            {"id": record_id},
        ).mappings().first()
    if record is None:
        return "missing"

    evidence = record["evidence"] or {}
    refs = evidence.get("refs") or []
    location = refs[0].get("location") if refs else None
    alert = render_eligibility_alert(
        company=record["company"], match_method=record["match_method"],
        confidence=float(record["confidence"]) if record["confidence"] is not None
        else None,
        evidence_location=location, source_group=record["group_name"],
    )
    key = dedup_key(f"elig:{record['id']}", f"state:{record['state']}")
    with engine.begin() as conn:
        notification_id, is_new = notify_records.insert_notification(
            conn, dedup_key=key, priority=NotificationPriority.HIGH,
            reason="Eligibility established — you are on the company's list",
            payload={"text": alert.text}, evidence_refs=alert.evidence_refs,
            eligibility_id=str(record["id"]),
        )
    if not is_new:
        return "suppressed_dedup"
    _enqueue_send(notification_id)
    logger.info("eligibility_notification_queued", record_id=record_id)
    return "queued"


def deadline_escalations() -> dict[str, int]:
    """FR-NOT-005: one reminder per crossed window per deadline value.
    Runs right after the P8 sweep (same cadence)."""
    settings = get_settings()
    now = datetime.now(tz=ZoneInfo(settings.app_timezone))
    engine = _engine()
    sent = 0
    with engine.begin() as conn:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT d.id AS deadline_id, d.due_at, d.state, d.reminder_policy, "
                "d.reminders_sent, e.id AS event_id, e.type, e.deadline_at, "
                "e.start_at, e.company_id, e.current_payload, "
                "c.canonical_name AS company, c.watch_state FROM deadlines d "
                "JOIN events e ON e.id = d.event_id "
                "LEFT JOIN companies c ON c.id = e.company_id "
                "WHERE d.state IN ('OPEN', 'DUE_SOON')"
            )
        ).mappings().all()
        channel = _channel()
        for row in rows:
            eligible = _company_eligible(conn, row["company_id"]
                                         and str(row["company_id"]))
            ctx = EventContext(
                event_id=str(row["event_id"]), event_type=EventType(row["type"]),
                deadline_at=row["deadline_at"], start_at=row["start_at"],
                has_company=row["company_id"] is not None,
                company_eligible=eligible,
                company_watching=(row["watch_state"] == "WATCHING"),
            )
            decision = priority_for_event(ctx, now)
            windows = reminder_windows(
                decision.priority,
                tuple((row["reminder_policy"] or {}).get("windows_hours") or (24, 6, 1)),
            )
            already = tuple(row["reminders_sent"] or [])
            crossed = windows_crossed(row["due_at"], now, windows, already)
            for window in crossed:
                state_hash = material_state_hash(
                    row["deadline_at"], row["start_at"],
                    (row["current_payload"] or {}).get("venue"),
                    (row["current_payload"] or {}).get("links") or [],
                    (row["current_payload"] or {}).get("designation"),
                    (row["current_payload"] or {}).get("salary_package"),
                )
                key = dedup_key(str(row["event_id"]), state_hash, "escalation", window)
                alert = render_reminder(
                    priority=decision.priority, company=row["company"],
                    event_type=EventType(row["type"]).value, due_at=row["due_at"],
                    window_hours=window,
                )
                notification_id, is_new = notify_records.insert_notification(
                    conn, dedup_key=key, priority=decision.priority,
                    reason=f"Escalation reminder (T-{window}h)",
                    payload={"text": alert.text}, evidence_refs=[],
                    event_id=str(row["event_id"]),
                )
                if not is_new:
                    continue
                try:
                    channel.send(settings.telegram_chat_id, alert.text)
                except DeliveryError as exc:
                    notify_records.record_delivery(
                        conn, notification_id, "telegram", ok=False, error=str(exc))
                    logger.warning("escalation_delivery_failed",
                                   deadline_id=str(row["deadline_id"]),
                                   error=str(exc)[:120])
                    continue  # window stays unsent — retried next sweep
                notify_records.mark_sent(conn, notification_id)
                notify_records.record_delivery(conn, notification_id, "telegram",
                                               ok=True, error=None)
                conn.execute(
                    sqlalchemy.text(
                        "UPDATE deadlines SET reminders_sent = "
                        "CAST(:sent AS jsonb), updated_at = now() "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(row["deadline_id"]),
                     "sent": json.dumps(list(already) + [window])},
                )
                sent += 1
    if sent:
        logger.info("escalation_reminders_sent", count=sent)
    return {"reminders_sent": sent}


def daily_digest() -> str:
    """F-025: compose the digest from PENDING MEDIUM/LOW notifications only
    (canonical events); empty digest is suppressed (FR-NOT-006)."""
    settings = get_settings()
    engine = _engine()
    with engine.connect() as conn:
        items = notify_records.pending_digest_items(conn)
    if not items:
        logger.info("digest_empty_suppressed")
        return "empty"

    sections: dict[str, list[str]] = {}
    ids: list[str] = []
    for item in items:
        event_type = (item["event_type"] or "OTHER").replace("_", " ").title()
        company = item["company"] or "General"
        topic = f"{company} · {event_type}"
        sections.setdefault(topic, []).append(
            (item["payload"] or {}).get("reason") or item["reason"]
        )
        ids.append(str(item["id"]))

    text = render_digest(list(sections.items()))
    if text is None:
        logger.info("digest_empty_suppressed")
        return "empty"

    with engine.begin() as conn:
        try:
            _channel().send(settings.telegram_chat_id, text)
        except DeliveryError as exc:
            logger.warning("digest_delivery_failed", error=str(exc)[:120])
            return "delivery_failed"
        notify_records.mark_digest_sent(conn, ids, "digest")
    logger.info("digest_sent", items=len(ids), sections=len(sections))
    return f"sent:{len(ids)}"
