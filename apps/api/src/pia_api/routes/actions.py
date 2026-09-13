"""P13/F-030 approval workflow API (ADR-008, SEC-004) — proposals in, decisions out.

- GET  /api/v1/actions?status=     — action rows (+ live-computed pre-fill)
- POST /api/v1/actions/{id}/approve — WAITING_APPROVAL -> APPROVED
- POST /api/v1/actions/{id}/reject  — WAITING_APPROVAL -> REJECTED

Every transition is guarded by the §10.4 state machine and written to
audit_logs (SEC-004). There is deliberately NO execution endpoint (SEC-005):
APPROVED is terminal until the P14 submission executor lands behind its own
ADR; ACTION_AUTOMATION_ENABLED stays false. Pre-fill is computed at read time
from the profile (SEC-002: decrypted identifiers never persist in actions)."""

import json

import sqlalchemy
from fastapi import APIRouter, Depends, HTTPException, Query

from pia_api.db import get_engine
from pia_api.deps import require_dashboard_token
from pia_shared.crypto import decrypt
from pia_shared.enums import ActionStatus
from pia_shared.states import InvalidTransitionError, assert_valid_transition

router = APIRouter(
    prefix="/api/v1", tags=["actions"], dependencies=[Depends(require_dashboard_token)]
)

_SENSITIVE = ("roll_number", "registration_number", "student_id")


def _audit(
    conn: sqlalchemy.Connection, action: str, entity_id: str, metadata: dict
) -> None:
    conn.execute(
        sqlalchemy.text(
            "INSERT INTO audit_logs (actor, action, entity_type, entity_id, result, metadata) "
            "VALUES ('user', :action, 'action', CAST(:eid AS uuid), 'ok', CAST(:meta AS jsonb))"
        ),
        {"action": action, "eid": entity_id, "meta": json.dumps(metadata, default=str)},
    )


def _prefill(conn: sqlalchemy.Connection) -> dict | None:
    """The owner's placement data, decrypted for display only (SEC-002 read
    path — the same one GET /profile uses). None when no profile exists."""
    row = conn.execute(
        sqlalchemy.text(
            "SELECT p.canonical_name, p.roll_number, p.registration_number, "
            "p.student_id, p.branch, p.batch, p.cgpa, p.tenth_percent, "
            "p.twelfth_percent, p.backlog_count, u.display_name, u.email "
            "FROM candidate_profiles p JOIN users u ON u.id = p.user_id LIMIT 1"
        )
    ).mappings().first()
    if row is None:
        return None
    from pia_api.settings import get_settings

    secret = get_settings().pia_encryption_key
    data = dict(row)
    for field in _SENSITIVE:
        data[field] = decrypt(data[field], secret)
    return {
        "full_name": data["canonical_name"],
        "email": data["email"],
        "roll_number": data["roll_number"],
        "registration_number": data["registration_number"],
        "student_id": data["student_id"],
        "branch": data["branch"],
        "batch": data["batch"],
        "cgpa": data["cgpa"],
        "tenth_percent": data["tenth_percent"],
        "twelfth_percent": data["twelfth_percent"],
        "backlog_count": data["backlog_count"],
    }


def _load_action(conn: sqlalchemy.Connection, action_id: str) -> dict:
    row = conn.execute(
        sqlalchemy.text(
            "SELECT a.id, a.type, a.target, a.payload, a.risk_level, "
            "a.status::text AS status, a.approval_required, a.created_at, "
            "a.updated_at, e.id AS event_id, e.title AS event_title, "
            "c.canonical_name AS company FROM actions a "
            "LEFT JOIN events e ON e.id = CAST(a.payload->>'event_id' AS uuid) "
            "LEFT JOIN companies c ON c.id = e.company_id "
            "WHERE a.id = CAST(:id AS uuid)"
        ),
        {"id": action_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="action not found")
    return dict(row)


def _render(row: dict, prefill: dict | None) -> dict:
    body = {k: v for k, v in row.items() if k not in ("event_id", "event_title", "company")}
    body["event"] = (
        {"id": row["event_id"], "title": row["event_title"], "company": row["company"]}
        if row.get("event_id") else None
    )
    body["prefill"] = prefill if row["type"] == "form_draft" else None
    return body


@router.get("/actions")
def list_actions(
    status: str | None = Query(default=None),
) -> dict:
    if status is not None and status not in [s.value for s in ActionStatus]:
        raise HTTPException(status_code=422, detail=f"unknown status: {status}")
    engine = get_engine()
    with engine.connect() as conn:
        sql = (
            "SELECT a.id, a.type, a.target, a.payload, a.risk_level, "
            "a.status::text AS status, a.approval_required, a.created_at, "
            "a.updated_at, e.id AS event_id, e.title AS event_title, "
            "c.canonical_name AS company FROM actions a "
            "LEFT JOIN events e ON e.id = CAST(a.payload->>'event_id' AS uuid) "
            "LEFT JOIN companies c ON c.id = e.company_id"
        )
        params: dict[str, object] = {}
        if status is not None:
            sql += " WHERE a.status::text = :status"
            params["status"] = status
        sql += " ORDER BY a.created_at DESC LIMIT 100"
        rows = [dict(r) for r in conn.execute(sqlalchemy.text(sql), params).mappings().all()]
        prefill = _prefill(conn)
    return {"actions": [_render(r, prefill) for r in rows]}


def _decide(action_id: str, to_status: ActionStatus) -> dict:
    engine = get_engine()
    with engine.begin() as conn:
        row = _load_action(conn, action_id)
        current = ActionStatus(row["status"])
        try:
            assert_valid_transition("action", current, to_status)
        except InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        conn.execute(
            sqlalchemy.text(
                "UPDATE actions SET status = :status, updated_at = now() "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"status": to_status.value, "id": action_id},
        )
        _audit(conn, f"action.{to_status.value.lower()}", action_id,
               {"from": current.value, "to": to_status.value, "target": row["target"]})
        row["status"] = to_status.value
    with engine.connect() as conn:
        prefill = _prefill(conn)
    return _render(row, prefill)


@router.post("/actions/{action_id}/approve")
def approve_action(action_id: str) -> dict:
    """WAITING_APPROVAL -> APPROVED. Terminal until the P14 executor exists —
    approval is recorded, audited, and the draft is kept for it to consume."""
    return _decide(action_id, ActionStatus.APPROVED)


@router.post("/actions/{action_id}/reject")
def reject_action(action_id: str) -> dict:
    """WAITING_APPROVAL -> REJECTED (terminal; a re-proposal needs a new draft)."""
    return _decide(action_id, ActionStatus.REJECTED)
