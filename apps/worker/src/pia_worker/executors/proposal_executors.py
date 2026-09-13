"""ADR-013 Stage 3 — executors for APPROVED reviewer proposals.

The deterministic end of the approval workflow: the reviewer proposes, the
owner approves on the dashboard, and THESE fixed programs do the work —
nothing here is LLM-driven, and everything is audited (SEC-004). Per
capability, gated by STAGE3_EXECUTORS_ENABLED (ADR-013):

- deadline_nudge / kyc_reminder / follow_up  → a Telegram reminder card
- verify_field  → apply payload.correction {event_id, field, value} onto the
  event record (field allowlist; the owner saw the exact change pre-approval)
- data_quality  → clear the message's event idempotency key and re-enqueue
  the deterministic event extractor (a re-parse, nothing invented)

Every run walks APPROVED -> EXECUTING -> SUCCEEDED | FAILED on the §10.4
machine with the reason recorded. DEC-008 untouched: KYC reminders are
Telegram messages, never automation of the session itself.
"""

import json

import sqlalchemy
import structlog

from pia_shared.enums import ActionStatus
from pia_worker.automation.executor import _transition
from pia_worker.db import engine_for_current_host
from pia_worker.settings import get_settings
from pia_worker.teams.listener import _telegram_send

logger = structlog.get_logger()

# Proposal types this module executes (form_draft has its own executor in
# automation/executor.py — ADR-012).
EXECUTABLE_TYPES = {"deadline_nudge", "kyc_reminder", "follow_up",
                    "verify_field", "data_quality"}
_CORRECTABLE_FIELDS = ("designation", "salary_package", "job_location",
                       "eligibility_note")


def _load_action(engine, action_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            sqlalchemy.text(
                "SELECT id, type, status::text AS status, target, payload "
                "FROM actions WHERE id = CAST(:id AS uuid)"
            ),
            {"id": action_id},
        ).mappings().first()
    return dict(row) if row else None


def _telegram_note(row: dict) -> dict:
    """deadline_nudge / kyc_reminder / follow_up: the approved reminder goes
    out as a Telegram card (pinned in chat history, not a silent state change)."""
    payload = row["payload"] or {}
    emoji = {"kyc_reminder": "🪪", "deadline_nudge": "⏰"}.get(row["type"], "📌")
    _telegram_send(text=(
        f"{emoji} <b>Reminder (you approved this)</b>\n"
        f"{payload.get('title') or row['target']}\n"
        f"💡 {payload.get('reason')}\n"
        "<i>Rejected proposals of the same target won't come back — that's "
        "how you silence a topic.</i>"))
    return {"sent": "telegram_note"}


def _event_correction(engine, row: dict) -> dict:
    """verify_field: apply the approved field correction onto the event."""
    correction = (row["payload"] or {}).get("correction") or {}
    field = str(correction.get("field") or "")
    value = str(correction.get("value") or "")
    event_id = str(correction.get("event_id") or "")
    if field not in _CORRECTABLE_FIELDS or not event_id or not value:
        raise RuntimeError("correction payload incomplete or field not allowed")
    with engine.begin() as conn:
        before = conn.execute(
            sqlalchemy.text(
                "SELECT current_payload->>:field AS current, "
                "(SELECT 1 FROM events WHERE id = CAST(:eid AS uuid)) AS exists_ "
                "FROM events WHERE id = CAST(:eid AS uuid)"
            ),
            {"field": field, "eid": event_id},
        ).mappings().first()
        if before is None or not before["exists_"]:
            raise RuntimeError(f"event {event_id[:8]} not found")
        conn.execute(
            sqlalchemy.text(
                "UPDATE events SET current_payload = jsonb_set("
                "current_payload, ARRAY[:field], to_jsonb(:value::text), true), "
                "updated_at = now() WHERE id = CAST(:eid AS uuid)"
            ),
            {"field": field, "value": value, "eid": event_id},
        )
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO audit_logs (actor, action, entity_type, entity_id, "
                "result, metadata) VALUES ('owner-correction', 'event.field.set', "
                "'event', CAST(:eid AS uuid), 'ok', CAST(:meta AS jsonb))"
            ),
            {"eid": event_id,
             "meta": json.dumps({"field": field, "before": before["current"],
                                 "after": value, "via": "approved proposal"})},
        )
    return {"corrected": f"{field} -> {value[:60]}", "event": event_id[:8]}


def _reparse(engine, row: dict) -> dict:
    """data_quality: re-run the deterministic event extractor on the original
    message (clear the idempotency marker, re-enqueue the pipeline job)."""
    message_id = str((row["payload"] or {}).get("reparse_message_id") or "")
    if not message_id:
        raise RuntimeError("reparse payload missing message_id")
    with engine.begin() as conn:
        deleted = conn.execute(
            sqlalchemy.text(
                "DELETE FROM idempotency_keys WHERE key = :key RETURNING key"
            ),
            {"key": f"events:{message_id}"},
        ).scalar()
        if not deleted:
            raise RuntimeError(f"no event extraction recorded for message "
                               f"{message_id[:8]}")
    from pia_worker.queue import DEFAULT_QUEUE, make_queue

    make_queue(DEFAULT_QUEUE).enqueue(
        "pia_worker.jobs.extract_events.extract_events", message_id)
    return {"reparse_enqueued": message_id[:8]}


def run_proposal_executor(action_id: str) -> str:
    """RQ entry: execute one approved reviewer proposal. Returns a short
    outcome string; every terminal state is audited."""
    settings = get_settings()
    if not settings.stage3_executors_enabled:
        logger.warning("proposal_executor_disabled", action_id=action_id)
        return "disabled"
    engine = engine_for_current_host()
    row = _load_action(engine, action_id)
    if row is None:
        return "action_missing"
    if row["type"] not in EXECUTABLE_TYPES:
        return "skipped_type"
    if row["status"] != ActionStatus.APPROVED.value:
        return "skipped_state"

    _transition(engine, action_id, ActionStatus.EXECUTING, "approved proposal")
    try:
        if row["type"] == "verify_field":
            result = _event_correction(engine, row)
        elif row["type"] == "data_quality":
            result = _reparse(engine, row)
        else:  # deadline_nudge / kyc_reminder / follow_up
            result = _telegram_note(row)
    except Exception as exc:  # noqa: BLE001 — loud, audited failure
        logger.error("proposal_executor_failed", action_id=action_id,
                     error=str(exc)[:180])
        _transition(engine, action_id, ActionStatus.FAILED, str(exc)[:200])
        _telegram_send(text=f"❌ <b>Approved action failed</b> — "
                             f"{str(exc)[:200]}")
        return "failed"

    _transition(engine, action_id, ActionStatus.SUCCEEDED, json.dumps(result))
    if "sent" not in result:  # the note executor already messaged the owner
        _telegram_send(text=f"✅ <b>Approved action done</b> — "
                             f"{json.dumps(result, default=str)[:250]}")
    logger.info("proposal_executed", action_id=action_id, type=row["type"],
                result=json.dumps(result, default=str)[:120])
    return "executed"
