"""P13/F-030: propose approval-gated form_draft actions.

Two entry points feed the same draft pipeline:
- propose_form_action(event_id) — WhatsApp pipeline: a FORM/KYC event with a
  link (hooked from extract_events).
- propose_meeting_form_action(form_url, presenters, company) — the Teams
  listener: a form link relayed from the meeting chat (DEC-008 amendment);
  presenters are the caption-detected teacher names.

Every proposal is exactly what ADR-008/SEC-004 prescribe: one row in the
`actions` table (PROPOSED -> WAITING_APPROVAL via the §10.4 machine, audited).
The payload holds references only — pre-fill values are computed at READ time
from the encrypted profile (SEC-002: decrypted PII is never persisted into
actions). Nothing here executes anything: the P14 submission executor is the
only path to real-world effect, behind its own ADR (SEC-005;
ACTION_AUTOMATION_ENABLED stays false)."""

import json
from urllib.parse import quote

import sqlalchemy
import structlog

from pia_shared.enums import ActionStatus
from pia_shared.states import assert_valid_transition

logger = structlog.get_logger()

_PROPOSABLE_TYPES = ("FORM", "KYC")
_RETRYABLE_STATES = ("FAILED", "EXPIRED")  # only these may be re-proposed


def _database_url() -> str:
    """Host-run callers (the Teams listener) have no DATABASE_URL in .env —
    assemble it from the POSTGRES_* variables; compose maps 5432 to localhost.
    In-container callers get DATABASE_URL from compose and use it directly."""
    import os

    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    user = quote(os.environ.get("POSTGRES_USER", "pia"))
    password = quote(os.environ.get("POSTGRES_PASSWORD", "pia"))
    db = os.environ.get("POSTGRES_DB", "pia")
    return f"postgresql+psycopg://{user}:{password}@localhost:5432/{db}"


def _engine_for_current_host() -> sqlalchemy.Engine:
    return sqlalchemy.create_engine(_database_url())


def _propose(target: str, payload: dict) -> str:
    """Shared draft creation: dedup per target URL, insert PROPOSED, walk to
    WAITING_APPROVAL on the §10.4 machine, audit both hops. Returns an
    outcome string (job-friendly, extract_events-style)."""
    engine = _engine_for_current_host()
    user_id, existing = None, None
    with engine.connect() as conn:
        user_id = conn.execute(
            sqlalchemy.text("SELECT id FROM users ORDER BY created_at LIMIT 1")
        ).scalar()
        existing = conn.execute(
            sqlalchemy.text(
                "SELECT status::text AS status FROM actions WHERE target = :target "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"target": target},
        ).scalar()
    if user_id is None:
        logger.warning("action_propose_no_user", target=target[:60])
        return "skipped_no_user"
    if existing is not None and existing not in _RETRYABLE_STATES:
        return "already_proposed"

    with engine.begin() as conn:
        action_id = conn.execute(
            sqlalchemy.text(
                "INSERT INTO actions (user_id, type, target, payload, risk_level, "
                "status, approval_required) VALUES (:user_id, 'form_draft', :target, "
                "CAST(:payload AS jsonb), 'medium', 'PROPOSED', true) RETURNING id"
            ),
            {"user_id": user_id, "target": target,
             "payload": json.dumps(payload, default=str)},
        ).scalar()
        # The §10.4 machine owns the lifecycle — assert it even on creation.
        assert_valid_transition("action", ActionStatus.PROPOSED,
                                ActionStatus.WAITING_APPROVAL)
        conn.execute(
            sqlalchemy.text(
                "UPDATE actions SET status = 'WAITING_APPROVAL', updated_at = now() "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(action_id)},
        )
        for action, to_state in (("action.propose", "PROPOSED"),
                                 ("action.await_approval", "WAITING_APPROVAL")):
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO audit_logs (actor, action, entity_type, entity_id, "
                    "result, metadata) VALUES ('worker', :action, 'action', "
                    "CAST(:id AS uuid), 'ok', CAST(:meta AS jsonb))"
                ),
                {"action": action, "id": str(action_id),
                 "meta": json.dumps({"to": to_state, "target": target})},
            )
    logger.info("form_action_proposed", action_id=str(action_id), target=target[:60])
    return "proposed"


def propose_form_action(event_id: str) -> str:
    """WhatsApp-pipeline entry: propose from a FORM/KYC event's first link."""
    engine = _engine_for_current_host()
    with engine.connect() as conn:
        event = conn.execute(
            sqlalchemy.text(
                "SELECT e.type::text AS type, c.canonical_name AS company, "
                "e.current_payload->'links' AS links FROM events e "
                "LEFT JOIN companies c ON c.id = e.company_id "
                "WHERE e.id = CAST(:id AS uuid)"
            ),
            {"id": event_id},
        ).mappings().first()
    if event is None:
        return "event_missing"
    if event["type"] not in _PROPOSABLE_TYPES:
        return "skipped_type"
    links = [str(x) for x in (event["links"] or [])]
    if not links:
        return "skipped_no_link"
    target = links[0].rstrip("/")
    payload = {
        "source": "whatsapp_message",
        "event_id": event_id,
        "event_type": event["type"],
        "company": event["company"],
        "form_url": target,
    }
    return _propose(target, payload)


def propose_meeting_form_action(
    form_url: str, presenters: tuple[str, ...] | list[str] = (),
    company: str | None = None,
) -> str:
    """Teams-listener entry: a form link seen in the meeting chat becomes a
    draft. `presenters` are the caption-detected teacher names — the one field
    the feedback form needs that no profile table can supply."""
    if not form_url:
        return "skipped_no_link"
    target = form_url.rstrip("/")
    payload = {
        "source": "teams_meeting_chat",
        "form_url": target,
        "presenters": [p for p in presenters if p][:3],
        "company": company,
    }
    return _propose(target, payload)
