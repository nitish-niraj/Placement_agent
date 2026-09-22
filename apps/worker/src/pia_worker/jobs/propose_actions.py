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
import time
import urllib.parse

import httpx
import sqlalchemy
import structlog

from pia_shared.enums import ActionStatus
from pia_shared.formlinks import GLIDE, GOOGLE, MICROSOFT, TEAMS, detect_form_provider
from pia_shared.states import assert_valid_transition
from pia_worker.db import engine_for_current_host as _engine_for_current_host

logger = structlog.get_logger()

_PROPOSABLE_TYPES = ("FORM", "KYC")
_RETRYABLE_STATES = ("FAILED", "EXPIRED")  # only these may be re-proposed
_RESOLVE_TIMEOUT_S = 10.0
_RESOLVE_ATTEMPTS = 3  # transient DNS blips must not mint bogus drafts


def _fetch_final_url(url: str) -> str:
    """Follow a (possibly shortened) link to its final URL. Thin wrapper so
    unit tests can stub the network without touching httpx."""
    with httpx.Client(follow_redirects=True, max_redirects=5,
                      timeout=_RESOLVE_TIMEOUT_S) as client:
        return str(client.get(url).url)


def _canonicalize_form_link(url: str) -> str | None:
    """Short links hide the destination (a Teams meeting once posed as a form
    draft). Resolve to the canonical form URL; None when resolution PROVES it
    is not a fillable form (Teams launcher, unknown site). Google/Microsoft/
    Glide finals are all accepted — non-Google providers get drafts too (the
    pre-fill endpoint serves them a guided manual path). Network failures fail
    OPEN with the raw link — a draft must never be lost to a transient error
    (the pre-fill endpoint re-validates as backstop). Direct docs.google.com
    links skip the fetch."""
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname == "docs.google.com" and parsed.path.startswith("/forms/"):
        return url
    last_error: Exception | None = None
    for attempt in range(_RESOLVE_ATTEMPTS):
        try:
            final = _fetch_final_url(url)
            break
        except Exception as exc:  # noqa: BLE001 — retry transients
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    else:
        logger.warning("form_link_unresolvable", url=url[:80],
                       error=str(last_error)[:120])
        return url
    provider = detect_form_provider(final)
    if provider == GOOGLE:
        return final
    if provider in (MICROSOFT, GLIDE):
        # Tripwire: the first live specimen of a new provider lands here —
        # that is the moment to build its entry parser (not before).
        logger.info("form_provider_manual", provider=provider, url=url[:80])
        return final
    if provider == TEAMS:
        logger.warning("form_link_not_a_form", url=url[:80], resolved=final[:80])
    else:
        logger.warning("form_link_unknown_host", url=url[:80], resolved=final[:80])
    return None


def _propose(target: str, payload: dict, action_type: str = "form_draft") -> str:
    """Shared proposal creation: dedup per target, insert PROPOSED, walk to
    WAITING_APPROVAL on the §10.4 machine, audit both hops. action_type is
    'form_draft' for forms and a reviewer type for Stage 2 proposals.
    Returns an outcome string (job-friendly, extract_events-style)."""
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
                "status, approval_required) VALUES (:user_id, :type, :target, "
                "CAST(:payload AS jsonb), 'medium', 'PROPOSED', true) RETURNING id"
            ),
            {"user_id": user_id, "type": action_type, "target": target,
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
    """WhatsApp-pipeline entry: propose from a FORM/KYC event's first link.

    Application-aware: a strict list gate (attached CSV/image lacks the
    student) and a negative application answer (NOT_APPLIED/NOT_INTERESTED
    on the matched company+role) both skip the draft. Undecided states
    still propose — the notify path asks whether they applied.
    """
    from pia_worker.applications import records as app_records
    from pia_worker.notify import records as notify_records
    from pia_worker.notify.classify import (
        analyze_application_message,
        is_post_application_shaped,
    )

    engine = _engine_for_current_host()
    with engine.connect() as conn:
        event = conn.execute(
            sqlalchemy.text(
                "SELECT e.type::text AS type, e.company_id, "
                "c.canonical_name AS company, e.current_payload, "
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
    payload = event["current_payload"] or {}
    company_id = event["company_id"] and str(event["company_id"])
    with engine.connect() as conn:
        if app_records.message_list_gate(conn, payload.get("source_message_id"),
                                         company_id) == "not_found":
            logger.info("form_draft_suppressed_not_in_list", event_id=event_id)
            return "skipped_not_in_list"
        user_id = notify_records.get_user_id(conn)
        role_norm = app_records.normalize_role(payload.get("designation"))
        app_row, matched = app_records.resolve_match(
            conn, user_id, company_id, role_norm)
    if app_row is not None and matched:
        from pia_shared.enums import ApplicationStatus
        negative = ApplicationStatus(app_row["status"]) in (
            ApplicationStatus.NOT_APPLIED, ApplicationStatus.NOT_INTERESTED)
        shaped = is_post_application_shaped(
            analyze_application_message(payload.get("excerpt") or ""))
        if negative and (shaped or not (payload.get("excerpt") or "").strip()):
            # FORM/KYC drafts are inherently post-application requests; a
            # negative answer (or a role-less notice for one) skips the draft.
            logger.info("form_draft_suppressed_not_applied", event_id=event_id,
                        status=app_row["status"])
            return "skipped_not_applied"
    links = [str(x) for x in (event["links"] or [])]
    if not links:
        return "skipped_no_link"
    raw_target = links[0].rstrip("/")
    target = _canonicalize_form_link(raw_target)
    if target is None:
        return "skipped_not_a_form"
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
    raw_target = form_url.rstrip("/")
    target = _canonicalize_form_link(raw_target)
    if target is None:
        return "skipped_not_a_form"
    payload = {
        "source": "teams_meeting_chat",
        "form_url": target,
        "presenters": [p for p in presenters if p][:3],
        "company": company,
    }
    return _propose(target, payload)
