"""ADR-011 Stage 4 — reactive per-message judgment (the narrow form).

Where the deterministic classifier is weakest — messages it placed in
GENERAL/UNKNOWN with MEDIUM/LOW importance — the agent gets ONE bounded,
schema-validated second opinion: escalate (a Telegram heads-up), digest (the
daily reviewer sees it anyway), or ignore.

Guardrails (all enforced in code, ADR-004 posture):
- OFF by default: STAGE4_REACTIVE_ENABLED=false — the fixed pipeline already
  performs this well; enable only if misclassified alerts are observed.
- Rule veto: CRITICAL/HIGH messages never reach this (they already notify).
- Confidence threshold: escalation requires confidence >= threshold.
- Daily cap: at most STAGE4_ESCALATION_CAP escalations per day (Redis counter).
- Advisory only: a judgment never mutates eligibility/events state; it sends
  a message and writes an audit row, nothing else.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import sqlalchemy
import structlog

from pia_shared.schemas import ReactiveJudgment
from pia_worker.ai.provider import NIMProvider, ProviderError
from pia_worker.db import engine_for_current_host
from pia_worker.settings import get_settings
from pia_worker.teams.listener import _telegram_send

logger = structlog.get_logger()

_ESCALATION_COUNTER = "stage4:escalations:{date}"


JUDGMENT_SYSTEM = """You are PIA's reactive judge for ONE student's placement
messages. The deterministic classifier could not confidently place this
message. Judge it: does it deserve an immediate Telegram heads-up?
- escalate: placement-relevant and time-sensitive (a drive, deadline, form,
  session, shortlist, or similar the student must act on soon).
- digest: placement-relevant but not urgent (the daily reviewer sees it).
- ignore: academic noise, chit-chat, or anything not actionable.
Be conservative: when unsure, prefer digest or ignore. reason must quote the
exact words from the message that justify the verdict."""


def _escalations_today(redis_client, tz) -> int:
    today = datetime.now(tz=tz).strftime("%Y-%m-%d")
    value = redis_client.get(_ESCALATION_COUNTER.format(date=today))
    return int(value) if value else 0


def _bump_escalations(redis_client, tz, cap: int) -> bool:
    """Increments the daily counter; False when the cap is already reached."""
    today = datetime.now(tz=tz).strftime("%Y-%m-%d")
    key = _ESCALATION_COUNTER.format(date=today)
    count = redis_client.incr(key)
    redis_client.expire(key, 86400 * 2)
    return count <= cap


def judge_message(message_id: str) -> str:
    """RQ entry: one bounded second opinion for a hard-to-classify message.
    Returns a short outcome string; never raises to the queue."""
    settings = get_settings()
    if not settings.stage4_reactive_enabled:
        return "disabled"

    engine = engine_for_current_host()
    with engine.connect() as conn:
        row = conn.execute(
            sqlalchemy.text(
                "SELECT m.text, m.domain::text AS domain, "
                "m.importance::text AS importance FROM messages m "
                "WHERE m.id = CAST(:id AS uuid)"
            ),
            {"id": message_id},
        ).mappings().first()
    if row is None:
        return "message_missing"
    text = (row["text"] or "").strip()
    if not text:
        return "skipped_empty"
    # Rule veto: messages the pipeline already escalates never reach here, and
    # the judge may never DOWNGRADE anything the rules chose to notify.
    if row["importance"] in ("CRITICAL", "HIGH"):
        return "skipped_rule_veto"

    try:
        provider = NIMProvider()
        judgment: ReactiveJudgment = provider.complete_structured(
            task="reactive_judgment",
            system=JUDGMENT_SYSTEM,
            user=f"CLASSIFIED AS: domain={row['domain']}, "
                 f"importance={row['importance']}\n\nMESSAGE:\n{text[:1500]}",
            schema=ReactiveJudgment,
            correlation_id="",
            max_tokens=300,
        )[0]
    except ProviderError as exc:
        logger.warning("reactive_judgment_unavailable", error=str(exc)[:120])
        return "judgment_unavailable"

    verdict = judgment.verdict
    if verdict == "escalate" \
            and judgment.confidence < settings.stage4_confidence_threshold:
        verdict = "digest"  # below the confidence threshold — no heads-up
    if verdict == "escalate":
        from pia_worker.queue import make_connection

        tz = ZoneInfo(settings.app_timezone)
        redis_client = make_connection(settings.redis_url)
        if not _bump_escalations(redis_client, tz,
                                 settings.stage4_escalation_cap):
            logger.info("reactive_escalation_capped", message_id=message_id)
            _audit(engine, message_id, "capped", judgment)
            return "escalation_capped"
        _telegram_send(text=(
            "👀 <b>Possible missed alert</b> (second opinion)\n"
            f"{text[:400]}\n"
            f"💡 {judgment.reason[:200]}\n"
            "<i>Judged worth surfacing — verify on the dashboard Inbox.</i>"))

    _audit(engine, message_id, verdict, judgment)
    logger.info("reactive_judged", message_id=message_id,
                verdict=verdict, confidence=judgment.confidence)
    return f"judged:{verdict}"


def _audit(engine, message_id: str, verdict: str, judgment: ReactiveJudgment) -> None:
    """Every judgment lands in the audit trail (SEC-004), best-effort."""
    try:
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO audit_logs (actor, action, entity_type, entity_id, "
                    "result, metadata) VALUES ('stage4-judge', 'message.judged', "
                    "'message', CAST(:id AS uuid), :result, CAST(:meta AS jsonb))"
                ),
                {"id": message_id, "result": verdict,
                 "meta": json.dumps({"reason": judgment.reason[:200],
                                     "confidence": judgment.confidence},
                                    default=str)},
            )
    except Exception as exc:  # noqa: BLE001 — audit is best-effort here
        logger.warning("reactive_audit_failed", error=str(exc)[:120])
