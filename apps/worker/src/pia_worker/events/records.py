"""P8 event persistence (F-019/F-020/F-021) — canonical events, deadlines,
delta application. The pure pieces (canonical key, EventPlan, delta diff) live
in canonical.py; this module owns the DB writes.

Dedup posture (ADR-006, TRD §7 layer 3 — the general semantic engine is P9):
- identical canonical tuple          -> duplicate, nothing written
- same company+type(+action) near-match with changed material facts
                                     -> event_updates row + event/deadline update
- otherwise                          -> new canonical event
"""

import json
from datetime import datetime, timedelta

import sqlalchemy
import structlog

from pia_shared.enums import DeadlineState, EventStatus
from pia_shared.states import assert_valid_transition
from pia_worker.events.canonical import EventPlan, canonical_key, material_delta

logger = structlog.get_logger()


def _audit(conn: sqlalchemy.Connection, action: str, entity_id: str,
           metadata: dict) -> None:
    conn.execute(
        sqlalchemy.text(
            "INSERT INTO audit_logs (actor, action, entity_type, entity_id, result, "
            "metadata) VALUES ('worker:events', :action, 'events', "
            "CAST(:id AS uuid), 'ok', CAST(:meta AS jsonb))"
        ),
        {"action": action, "id": entity_id, "meta": json.dumps(metadata, default=str)},
    )


def upsert_event(
    conn: sqlalchemy.Connection, plan: EventPlan, message_id: str | None,
    group_id: str | None,
) -> tuple[str, str]:
    """Create or delta-update the canonical event for this plan.
    Returns (event_id, outcome) with outcome in created|delta|duplicate."""
    key = canonical_key(plan.company_key, plan.event_type, plan.action,
                        plan.deadline_at, plan.links[0] if plan.links else None)

    # Semantic near-match first (F-021): same company + type + action.
    near = conn.execute(
        sqlalchemy.text(
            "SELECT id, status, deadline_at, start_at, current_payload, canonical_key "
            "FROM events WHERE company_id IS NOT DISTINCT FROM "
            "CAST(:company AS uuid) AND type = CAST(:type AS event_type) "
            "AND COALESCE(current_payload->>'action', '') = COALESCE(:action, '') "
            "AND status IN ('DETECTED', 'ACTIVE') "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"company": plan.company_id, "type": plan.event_type.value,
         "action": plan.action},
    ).mappings().first()

    if near is not None:
        delta = material_delta(
            near["deadline_at"], near["start_at"], near["current_payload"] or {}, plan,
        )
        if not delta:
            # FR-DED-005: the suppression itself is part of the event history
            _audit(conn, "event.duplicate", str(near["id"]),
                   {"message_id": message_id,
                    "reason": "identical canonical fact tuple (FR-DED-003)"})
            return str(near["id"]), "duplicate"
        new_key = canonical_key(plan.company_key, plan.event_type, plan.action,
                                plan.deadline_at, plan.links[0] if plan.links else None)
        merged_payload = dict(near["current_payload"] or {})
        merged_payload.update(plan.payload())
        assert_valid_transition("event", EventStatus(near["status"]),
                                EventStatus.ACTIVE)  # ACTIVE -> ACTIVE delta
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO event_updates (event_id, source_message_id, delta) "
                "VALUES (CAST(:eid AS uuid), CAST(:mid AS uuid), CAST(:delta AS jsonb))"
            ),
            {"eid": str(near["id"]), "mid": message_id,
             "delta": json.dumps(delta, default=str)},
        )
        conn.execute(
            sqlalchemy.text(
                "UPDATE events SET status = CAST('ACTIVE' AS event_status), "
                "deadline_at = :deadline, start_at = :start_at, "
                "current_payload = CAST(:payload AS jsonb), canonical_key = :key, "
                "updated_at = now() WHERE id = CAST(:id AS uuid)"
            ),
            {"eid": str(near["id"]), "deadline": plan.deadline_at,
             "start_at": plan.start_at,
             "payload": json.dumps(merged_payload, default=str), "key": new_key,
             "id": str(near["id"])},
        )
        _audit(conn, "event.delta", str(near["id"]),
               {"delta": delta, "message_id": message_id})
        return str(near["id"]), "delta"

    event_id = str(conn.execute(
        sqlalchemy.text(
            "INSERT INTO events (company_id, group_id, type, status, canonical_key, "
            "title, current_payload, start_at, deadline_at) "
            "VALUES (CAST(:company AS uuid), CAST(:grp AS uuid), "
            "CAST(:type AS event_type), CAST('DETECTED' AS event_status), :key, "
            ":title, CAST(:payload AS jsonb), :start_at, :deadline) RETURNING id"
        ),
        {"company": plan.company_id, "grp": group_id,
         "type": plan.event_type.value, "key": key, "title": plan.title,
         "payload": json.dumps(plan.payload(), default=str),
         "start_at": plan.start_at, "deadline": plan.deadline_at},
    ).scalar_one())
    assert_valid_transition("event", EventStatus.DETECTED, EventStatus.ACTIVE)
    conn.execute(
        sqlalchemy.text(
            "UPDATE events SET status = CAST('ACTIVE' AS event_status), "
            "updated_at = now() WHERE id = CAST(:id AS uuid)"
        ),
        {"id": event_id},
    )
    _audit(conn, "event.created", event_id,
           {"type": plan.event_type.value, "company": plan.company_key,
            "canonical_key": key, "message_id": message_id})
    return event_id, "created"


def upsert_deadline(conn: sqlalchemy.Connection, event_id: str,
                    due_at: datetime) -> str:
    """FR-EVT-003: one deadline per event (UNIQUE); a delta just moves due_at.
    State transitions stay with the sweep job."""
    row = conn.execute(
        sqlalchemy.text(
            "INSERT INTO deadlines (event_id, due_at, state) "
            "VALUES (CAST(:eid AS uuid), :due, CAST('OPEN' AS deadline_state)) "
            "ON CONFLICT (event_id) DO UPDATE SET due_at = EXCLUDED.due_at, "
            "updated_at = now() RETURNING id"
        ),
        {"eid": event_id, "due": due_at},
    )
    return str(row.scalar_one())


def decide_deadline_state(
    current: str, due_at: datetime, now: datetime, windows_hours: tuple[int, ...],
) -> str | None:
    """Pure sweep decision (F-020): None = no change. Due-past wins over
    DUE_SOON; COMPLETED/CANCELLED are never chosen automatically."""
    if current in ("EXPIRED", "CANCELLED", "COMPLETED"):
        return None
    if now >= due_at:
        return "EXPIRED"
    first_window = max(windows_hours) if windows_hours else 24
    if current == "OPEN" and due_at - now <= timedelta(hours=first_window):
        return "DUE_SOON"
    return None


def sweep_deadlines(conn: sqlalchemy.Connection, now: datetime) -> dict[str, int]:
    """F-020 states: OPEN -> DUE_SOON inside the first reminder window,
    anything unfinished -> EXPIRED past due. Machine-asserted + audited."""
    rows = conn.execute(
        sqlalchemy.text(
            "SELECT d.id, d.state, d.due_at, d.reminder_policy FROM deadlines d "
            "WHERE d.state IN ('OPEN', 'DUE_SOON')"
        )
    ).mappings().all()
    counts = {"due_soon": 0, "expired": 0}
    for row in rows:
        windows = tuple(
            (row["reminder_policy"] or {}).get("windows_hours") or (24, 6, 1)
        )
        target = decide_deadline_state(row["state"], row["due_at"], now, windows)
        if target is None:
            continue
        assert_valid_transition("deadline", DeadlineState(row["state"]),
                                DeadlineState(target))
        conn.execute(
            sqlalchemy.text(
                "UPDATE deadlines SET state = CAST(:s AS deadline_state), "
                "updated_at = now() WHERE id = CAST(:id AS uuid)"
            ),
            {"s": target, "id": str(row["id"])},
        )
        _audit(conn, "deadline.state_change", str(row["id"]),
               {"from": row["state"], "to": target, "due_at": row["due_at"].isoformat()})
        counts["due_soon" if target == "DUE_SOON" else "expired"] += 1
    return counts
