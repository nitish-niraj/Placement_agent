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
    ApplicationStatus,
    EventType,
    NotificationPriority,
    NotificationStatus,
)
from pia_shared.metrics import increment
from pia_worker.applications import records as app_records
from pia_worker.jobs.process_message import _engine
from pia_worker.notify import records as notify_records
from pia_worker.notify.buttons import ask_applied_keyboard
from pia_worker.notify.channel import DeliveryError, TelegramChannel
from pia_worker.notify.classify import (
    APPLICATION_GATED_TYPES,
    LIST_GATED_TYPES,
    FollowUpDecision,
    analyze_application_message,
    decide_application_followup,
    is_post_application_shaped,
)
from pia_worker.notify.decide import (
    Decision,
    EventContext,
    dedup_key,
    material_state_hash,
    priority_for_event,
    reminder_windows,
    windows_crossed,
)
from pia_worker.notify.render import (
    render_ask_applied,
    render_eligibility_alert,
    render_event,
    render_event_updated,
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
            "c.normalized_key AS company_key, d.state AS deadline_state, "
            "c.watch_state, g.name AS group_name FROM events e "
            "LEFT JOIN companies c ON c.id = e.company_id "
            "LEFT JOIN groups g ON g.id = e.group_id "
            "LEFT JOIN deadlines d ON d.event_id = e.id "
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


def _max_priority(first: NotificationPriority,
                  second: NotificationPriority) -> NotificationPriority:
    """Urgency wins: ladder signals (deadline TODAY) and application signals
    (cancellation penalty) each escalate; neither may downgrade the other."""
    order = (NotificationPriority.LOW, NotificationPriority.MEDIUM,
             NotificationPriority.HIGH, NotificationPriority.CRITICAL)
    return first if order.index(first) >= order.index(second) else second


def _application_gate(
    conn: sqlalchemy.Connection, *, event_type: EventType,
    company_id: str | None, payload: dict,
) -> tuple[str, FollowUpDecision | None, dict | None]:
    """Application-aware gate for one event notification.

    Returns (verdict, decision, app_row) with verdict in:
    - "proceed" — legacy path unchanged (ungated type, no company,
      opening/informational shape).
    - "proceed_boost" — APPLIED + actionable: proceed, priority floored at
      the application decision's priority (penalty => HIGH).
    - "ask" — undecided status + actionable message: send the
      ask-whether-applied nudge instead of a follow-up.
    - "suppressed_list" — strict CSV/image gate: attached list lacks the
      student (Mars.AI-class spam ends here).
    - "suppressed_state" — answered NOT_APPLIED/NOT_INTERESTED (or role
      mismatch): post-application follow-up suppressed.
    """
    nodecision: FollowUpDecision | None = None
    if event_type not in APPLICATION_GATED_TYPES or not company_id:
        return "proceed", nodecision, None
    message_id = payload.get("source_message_id")
    if (event_type in LIST_GATED_TYPES
            and app_records.message_list_gate(
                conn, message_id, str(company_id)) == "not_found"):
        return "suppressed_list", nodecision, None
    text = (payload.get("excerpt") or "")
    analysis = analyze_application_message(text)
    if not is_post_application_shaped(analysis):
        return "proceed", nodecision, None  # openings/digest unchanged
    user_id = notify_records.get_user_id(conn)
    role_norm = app_records.normalize_role(payload.get("designation"))
    row, matched = app_records.resolve_match(
        conn, user_id, str(company_id), role_norm)
    status = ApplicationStatus(row["status"]) if row else ApplicationStatus.UNKNOWN
    decision = decide_application_followup(
        status=status, analysis=analysis, role_matched=matched)
    if decision.should_follow_up:
        return "proceed_boost", decision, row
    if decision.suggest_ask_applied:
        return "ask", decision, row
    return "suppressed_state", decision, row


def _enqueue_send(notification_id: str) -> None:
    from redis import Redis
    from rq import Queue, Retry

    from pia_worker.queue import REALTIME_QUEUE
    from pia_worker.settings import get_settings

    settings = get_settings()
    Queue(REALTIME_QUEUE, connection=Redis.from_url(settings.redis_url)).enqueue(
        "pia_worker.jobs.notify.send_notification", notification_id,
        retry=Retry(max=settings.notify_send_attempts),
    )


def _build_resolved_context(engine, event: dict, payload: dict,  # noqa: ANN001
                            eligible: bool, app_row: dict | None,
                            decision_reason: str = ""):
    """Context-first object for one event notification: subject, topic,
    verification, deadline truth, recommendation + evidence chain."""
    from pia_worker.notify.context import (
        AttachmentVerification,
        build_context,
    )
    from pia_worker.notify.topics import classify_topic

    message_id = payload.get("source_message_id")
    company_id = event["company_id"] and str(event["company_id"])
    with engine.connect() as conn:
        verification, detail = app_records.resolve_verification(
            conn, message_id, company_id)
    excerpt = payload.get("excerpt") or ""
    topic = classify_topic(excerpt)
    subject = (payload.get("subject_display") or event["company"]
               or "📋 Placement Update")
    role = payload.get("designation")
    location = payload.get("venue") or payload.get("job_location")
    status = (ApplicationStatus(app_row["status"]) if app_row
              else ApplicationStatus.UNKNOWN)
    return build_context(
        event_id=str(event["id"]), company=event["company"],
        subject_display=subject, role=role,
        event_type=str(event["type"]), topic=topic,
        action_required=is_post_application_shaped(
            analyze_application_message(excerpt)),
        eligibility_status="ELIGIBLE" if eligible else "UNKNOWN",
        application_status=status.value,
        deadline=event["deadline_at"], location=location,
        source_message=excerpt,
        source_message_id=message_id, source_group=event["group_name"],
        attachments=(),
        verification=AttachmentVerification(verification),
        attachment_detail=detail,
        confidence=1.0 if event["company_id"] else 0.5,
        reason=decision_reason,
    )


def _latest_delta(engine, event_id: str,  # noqa: ANN001
                  message_id: str | None) -> dict:
    """Newest material delta for this event (prefer the triggering message's
    row). Empty dict = describe the event itself instead."""
    with engine.connect() as conn:
        if message_id:
            row = conn.execute(
                sqlalchemy.text(
                    "SELECT delta FROM event_updates "
                    "WHERE event_id = CAST(:eid AS uuid) "
                    "AND source_message_id = CAST(:mid AS uuid) "
                    "ORDER BY detected_at DESC LIMIT 1"
                ),
                {"eid": event_id, "mid": message_id},
            ).mappings().first()
            if row is not None and row["delta"]:
                return dict(row["delta"])
        row = conn.execute(
            sqlalchemy.text(
                "SELECT delta FROM event_updates "
                "WHERE event_id = CAST(:eid AS uuid) "
                "ORDER BY detected_at DESC LIMIT 1"
            ),
            {"eid": event_id},
        ).mappings().first()
    return dict(row["delta"]) if row is not None and row["delta"] else {}


def _send_ask_nudge(engine, event: dict, payload: dict, state_hash: str,  # noqa: ANN001
                    outcome: str, app_row: dict | None) -> str:
    """Ask-whether-applied nudge with answer buttons. One per event state
    (dedup kind "ask_applied"); the answer persists the opportunity row, so
    repeat messages after an answer never re-ask."""
    with engine.begin() as conn:
        user_id = notify_records.get_user_id(conn)
        company_id = str(event["company_id"])
        if app_row is None:
            app_row = app_records.ensure_application(
                conn, user_id=user_id, company_id=company_id,
                company_normalized=str(event.get("company_key") or ""),
                role_normalized=app_records.normalize_role(
                    payload.get("designation")),
                source="ask_nudge")
        keyboard = ask_applied_keyboard(str(app_row["id"]))
        role = payload.get("designation")
        rendered = render_ask_applied(
            company=event["company"], role=role,
            event_type=str(event["type"]),
            context_line=f"{event.get('group_name') or 'group'} · "
                         f"msg {payload.get('source_message_id') or event['id']}")
        notification_id, is_new = notify_records.insert_notification(
            conn, dedup_key=dedup_key(str(event["id"]), state_hash,
                                      kind="ask_applied"),
            priority=NotificationPriority.MEDIUM,
            reason="application status undecided — asking whether applied",
            payload={"text": rendered.text, "reply_markup": keyboard,
                     "outcome": outcome, "nudge": "ask_applied"},
            evidence_refs=rendered.evidence_refs, event_id=str(event["id"]),
            message_id=payload.get("source_message_id"),
        )
    if not is_new:
        logger.info("ask_nudge_suppressed_dedup", event_id=str(event["id"]))
        return "suppressed_dedup"
    _enqueue_send(notification_id)
    logger.info("ask_nudge_queued", event_id=str(event["id"]))
    return "asked_applied"


def notify_event(event_id: str, outcome: str = "created") -> str:
    engine = _engine()
    with engine.connect() as conn:
        event = _load_event(conn, event_id)
        if event is None:
            logger.warning("notify_event_missing", event_id=event_id)
            return "missing"
        eligible = _company_eligible(conn, event["company_id"] and str(event["company_id"]))
    if not get_settings().notification_enabled:
        logger.info("notification_disabled_by_flag", event_id=event_id)
        return "disabled"

    payload = event["current_payload"] or {}
    state_hash = material_state_hash(
        event["deadline_at"], event["start_at"], payload.get("venue"),
        payload.get("links") or [], payload.get("designation"),
        payload.get("salary_package"),
    )
    key = dedup_key(str(event["id"]), state_hash)

    # Application-aware gate (company mentioned != applied) + strict
    # CSV/image list gate. Openings and informational shapes return
    # "proceed" and ride the legacy ladder below, unchanged.
    with engine.connect() as conn:
        verdict, app_decision, app_row = _application_gate(
            conn, event_type=EventType(event["type"]),
            company_id=event["company_id"] and str(event["company_id"]),
            payload=payload)
    if verdict in ("suppressed_list", "suppressed_state"):
        reason = ("attached eligibility list does not contain the student — "
                  "strictly ignored" if verdict == "suppressed_list"
                  else str(getattr(app_decision, "reason", "not applied")))
        with engine.begin() as conn:
            notify_records.insert_notification(
                conn, dedup_key=key, priority=NotificationPriority.LOW,
                reason=f"suppressed: {reason}",
                payload={"outcome": outcome, "gate": verdict},
                evidence_refs=[], event_id=str(event["id"]),
                message_id=payload.get("source_message_id"),
                status=NotificationStatus.SUPPRESSED,
            )
        logger.info("notification_suppressed_application",
                    event_id=event_id, verdict=verdict)
        return f"suppressed_{verdict.split('_', 1)[1]}"
    if verdict == "ask":
        return _send_ask_nudge(engine, event, payload, state_hash,
                               outcome, app_row)

    ctx = EventContext(
        event_id=str(event["id"]),
        event_type=EventType(event["type"]),
        deadline_at=event["deadline_at"],
        start_at=event["start_at"],
        has_company=event["company_id"] is not None,
        company_eligible=eligible,
        company_watching=(event["watch_state"] == "WATCHING"),
        deadline_state=str(event["deadline_state"])
        if event["deadline_state"] is not None else None,
    )
    decision = priority_for_event(ctx)
    if verdict == "proceed_boost" and app_decision is not None:
        # APPLIED + actionable: urgency wins both ways — a cancellation
        # penalty escalates even a ladder-MEDIUM event, and a ladder-CRITICAL
        # deadline keeps its rank.
        boosted = _max_priority(decision.priority, app_decision.priority)
        decision = Decision(boosted, "immediate" if boosted in (
            NotificationPriority.CRITICAL, NotificationPriority.HIGH)
            else "digest",
            decision.reason + " | applied + actionable: " + app_decision.reason)

    # Master §11 policy flags (env): NOTIFY_CRITICAL_IMMEDIATELY /
    # NOTIFY_MEDIUM_IN_DIGEST — deployment owners may route critical items to
    # the digest or switch digest items off entirely.
    settings = get_settings()
    delivery = decision.delivery
    if delivery == "immediate" and not settings.notify_critical_immediately:
        delivery = "digest"
    if delivery == "digest" and not settings.notify_medium_in_digest:
        with engine.begin() as conn:
            notify_records.insert_notification(
                conn, dedup_key=key, priority=decision.priority,
                reason=decision.reason + " (suppressed: digest disabled by policy)",
                payload={"outcome": outcome, "policy": "NOTIFY_MEDIUM_IN_DIGEST=false"},
                evidence_refs=[], event_id=str(event["id"]),
                message_id=payload.get("source_message_id"),
                status=NotificationStatus.SUPPRESSED,
            )
        logger.info("notification_suppressed_policy", event_id=event_id)
        return "suppressed_policy"

    if delivery == "digest":
        digest_ctx = _build_resolved_context(
            engine, event, payload, eligible, app_row,
            decision_reason=decision.reason)
        with engine.begin() as conn:
            notification_id, is_new = notify_records.insert_notification(
                conn, dedup_key=key, priority=decision.priority,
                reason=f"[{digest_ctx.stage}] {digest_ctx.recommendation_text}",
                payload={"outcome": outcome,
                         "subject": digest_ctx.subject_display,
                         "stage": digest_ctx.stage,
                         "topic": digest_ctx.topic.value,
                         "deadline": digest_ctx.deadline.isoformat()
                         if digest_ctx.deadline else None,
                         "recommendation": digest_ctx.recommendation.value,
                         "recommendation_text": digest_ctx.recommendation_text,
                         "reason": digest_ctx.reason},
                evidence_refs=[dict(r) for r in digest_ctx.evidence_refs],
                event_id=str(event["id"]),
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

    # CRITICAL/HIGH: build the context-first object, render the
    # student-facing template, persist, deliver via the retried job.
    resolved = _build_resolved_context(
        engine, event, payload, eligible, app_row,
        decision_reason=decision.reason)
    links = tuple(payload.get("links") or [])
    if outcome == "delta":
        delta = _latest_delta(engine, str(event["id"]),
                              payload.get("source_message_id"))
        alert = render_event_updated(resolved, delta, links)
    else:
        alert = render_event(resolved, links, payload.get("salary_package"))
    with engine.begin() as conn:
        notification_id, is_new = notify_records.insert_notification(
            conn, dedup_key=key, priority=decision.priority,
            reason=f"[{resolved.stage}] {resolved.recommendation_text}",
            payload={"text": alert.text, "outcome": outcome,
                     "subject": resolved.subject_display,
                     "stage": resolved.stage,
                     "topic": resolved.topic.value,
                     "recommendation": resolved.recommendation.value},
            evidence_refs=alert.evidence_refs, event_id=str(event["id"]),
            message_id=payload.get("source_message_id"),
        )
    if not is_new:
        logger.info("notification_deduplicated", event_id=event_id,
                    dedup_key=key[:12])
        return "suppressed_dedup"
    _enqueue_send(notification_id)
    logger.info("notification_created", event_id=event_id,
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
    if not settings.notification_enabled:
        logger.info("notification_disabled_by_flag",
                    notification_id=notification_id)
        return "disabled"

    try:
        result = _channel().send(settings.telegram_chat_id, row["payload"]["text"],
                                 reply_markup=row["payload"].get("reply_markup"))
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
                "r.user_id, r.company_id, c.canonical_name AS company, "
                "c.normalized_key AS company_key, g.name AS group_name "
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
    # Eligibility established -> track the opportunity as ELIGIBLE_NOT_APPLIED
    # and ask whether they applied (buttons persist the answer). Future
    # post-application messages gate on that answer.
    with engine.begin() as conn:
        app_row = app_records.ensure_application(
            conn, user_id=str(record["user_id"]),
            company_id=str(record["company_id"]),
            company_normalized=str(record["company_key"] or ""),
            source="eligibility",
            initial=ApplicationStatus.ELIGIBLE_NOT_APPLIED)
    ask_text = (alert.text + "\n\n<b>Have you applied for this role?</b> "
                "Tap below — future updates become follow-ups only if you have.")
    key = dedup_key(f"elig:{record['id']}", f"state:{record['state']}")
    with engine.begin() as conn:
        notification_id, is_new = notify_records.insert_notification(
            conn, dedup_key=key, priority=NotificationPriority.HIGH,
            reason="Eligibility established — you are on the company's list",
            payload={"text": ask_text,
                     "reply_markup": ask_applied_keyboard(str(app_row["id"]))},
            evidence_refs=alert.evidence_refs,
            eligibility_id=str(record["id"]),
        )
    if not is_new:
        return "suppressed_dedup"
    _enqueue_send(notification_id)
    logger.info("eligibility_notification_queued", record_id=record_id)
    return "queued"


def _escalation_gated(conn: sqlalchemy.Connection, row) -> bool:  # noqa: ANN001
    """True when this deadline reminder must not fire.

    Mirrors the notify-time gate with the text available on the row
    (excerpt, else title): the strict list gate kills FORM/KYC/REGISTRATION
    reminders for non-listed students; post-application-shaped reminders
    need APPLIED on the matched company+role. FORM/KYC/DOCUMENT_SUBMISSION
    are application-shaped by nature, so with no text they still require
    APPLIED; REGISTRATION/OA-type deadlines without post-application
    language are treated as openings and fire (legacy behavior).
    """
    event_type = EventType(row["type"])
    company_id = row["company_id"] and str(row["company_id"])
    if company_id is None or event_type not in APPLICATION_GATED_TYPES:
        return False
    payload = row["current_payload"] or {}
    if (event_type in LIST_GATED_TYPES
            and app_records.message_list_gate(
                conn, payload.get("source_message_id"), company_id) == "not_found"):
        logger.info("escalation_suppressed_not_in_list",
                    event_id=str(row["event_id"]))
        return True
    text = (payload.get("excerpt") or "").strip()
    if text:
        shaped = is_post_application_shaped(analyze_application_message(text))
    else:
        shaped = event_type in (EventType.FORM, EventType.KYC,
                                EventType.DOCUMENT_SUBMISSION)
    if not shaped:
        return False
    user_id = notify_records.get_user_id(conn)
    app_row, matched = app_records.resolve_match(
        conn, user_id, company_id,
        app_records.normalize_role(payload.get("designation")))
    status = (ApplicationStatus(app_row["status"]) if app_row
              else ApplicationStatus.UNKNOWN)
    if status is ApplicationStatus.APPLIED and matched:
        return False
    logger.info("escalation_suppressed_application",
                event_id=str(row["event_id"]), status=status.value,
                matched=matched)
    return True


def deadline_escalations() -> dict[str, int]:
    """FR-NOT-005: one reminder per crossed window per deadline value.
    Runs right after the P8 deadline sweep (same cadence)."""
    settings = get_settings()
    if not settings.notification_enabled:
        logger.info("escalations_disabled_by_flag")
        return {"reminders_sent": 0}
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
            if _escalation_gated(conn, row):
                continue
            ctx = EventContext(
                event_id=str(row["event_id"]), event_type=EventType(row["type"]),
                deadline_at=row["deadline_at"], start_at=row["start_at"],
                has_company=row["company_id"] is not None,
                company_eligible=eligible,
                company_watching=(row["watch_state"] == "WATCHING"),
                deadline_state=str(row["state"]),
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
    """F-025: categorized digest from PENDING MEDIUM/LOW notifications.

    Idempotent per calendar day: a `digest:{date}` anchor in idempotency_keys
    makes double-runs (retry-after-send, overlapping schedulers) a quiet
    "duplicate" instead of a second Telegram message. Empty digest stays
    suppressed (FR-NOT-006)."""
    from pia_worker.notify.render import render_digest_v2

    settings = get_settings()
    if not settings.digest_enabled:
        logger.info("digest_disabled_by_flag")
        return "disabled"
    engine = _engine()
    today = datetime.now(tz=ZoneInfo(settings.app_timezone)).date().isoformat()
    anchor = f"digest:{today}"
    with engine.begin() as conn:
        claimed = conn.execute(
            sqlalchemy.text(
                "INSERT INTO idempotency_keys (key, fingerprint, entity_ref) "
                "VALUES (:key, :fp, :ref) ON CONFLICT (key) DO NOTHING "
                "RETURNING key"
            ),
            {"key": anchor, "fp": anchor, "ref": "daily_digest"},
        ).first()
        if claimed is None:
            logger.info("digest_skipped_duplicate", date=today)
            return "duplicate"
    with engine.connect() as conn:
        items = notify_records.pending_digest_items(conn)
    if not items:
        logger.info("digest_empty_suppressed")
        return "empty"

    ids = [str(item["id"]) for item in items]
    text = render_digest_v2(today, [dict(item) for item in items])
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
    logger.info("digest_created", items=len(ids), date=today)
    return f"sent:{len(ids)}"
