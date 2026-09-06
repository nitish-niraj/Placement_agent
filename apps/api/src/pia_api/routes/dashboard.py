"""Dashboard API (P11, F-026/F-027) — read surface for the React cockpit.

Implements the remaining master §16 read endpoints plus the §3.2 additions.
All routes sit behind the dashboard bearer token (SEC-002 posture). The
company timeline merges events, event updates, eligibility records and
notifications into one evidence-backed history (FR-DED-005).
"""

import sqlalchemy
from fastapi import APIRouter, Depends, HTTPException

from pia_api.db import get_engine
from pia_api.deps import require_dashboard_token

router = APIRouter(prefix="/api/v1", tags=["dashboard"],
                   dependencies=[Depends(require_dashboard_token)])


@router.get("/overview")
def overview() -> dict:
    """Personal cockpit (master §17): counts, needs-review queue, next deadlines."""
    engine = get_engine()
    with engine.connect() as conn:
        counts = conn.execute(
            sqlalchemy.text(
                "SELECT (SELECT count(*) FROM messages) AS messages, "
                "(SELECT count(*) FROM events WHERE status = 'ACTIVE') AS events_active, "
                "(SELECT count(*) FROM deadlines WHERE state IN ('OPEN','DUE_SOON')) "
                "AS deadlines_open, "
                "(SELECT count(*) FROM eligibility_records WHERE state = 'ELIGIBLE') "
                "AS eligible_companies, "
                "(SELECT count(*) FROM notifications WHERE status = 'SENT' "
                "AND sent_at > now() - interval '24 hours') AS notifications_24h, "
                "(SELECT count(*) FROM notifications WHERE status = 'PENDING_DELIVERY') "
                "AS pending_delivery, "
                "(SELECT count(*) FROM eligibility_records "
                "WHERE requires_user_review) AS needs_review"
            )
        ).mappings().first()
        needs_review = conn.execute(
            sqlalchemy.text(
                "SELECT r.id, r.state, r.match_method, r.confidence, "
                "r.evidence->> 'rationale' AS reason, r.correction_note, "
                "c.canonical_name AS company FROM eligibility_records r "
                "JOIN companies c ON c.id = r.company_id "
                "WHERE r.requires_user_review ORDER BY r.detected_at DESC LIMIT 20"
            )
        ).mappings().all()
        deadlines = conn.execute(
            sqlalchemy.text(
                "SELECT d.id, d.due_at, d.state, e.type, c.canonical_name AS company "
                "FROM deadlines d JOIN events e ON e.id = d.event_id "
                "LEFT JOIN companies c ON c.id = e.company_id "
                "WHERE d.state IN ('OPEN', 'DUE_SOON') ORDER BY d.due_at LIMIT 8"
            )
        ).mappings().all()
    return {
        "counts": dict(counts) if counts else {},
        "needs_review": [dict(r) for r in needs_review],
        "deadlines": [dict(r) for r in deadlines],
    }


@router.get("/eligibility")
def list_eligibility(limit: int = 100) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT r.id, r.state, r.match_status, r.match_method, r.confidence, "
                "r.requires_user_review, r.correction_note, r.detected_at, r.evidence, "
                "c.canonical_name AS company, c.watch_state FROM eligibility_records r "
                "JOIN companies c ON c.id = r.company_id "
                "ORDER BY r.detected_at DESC LIMIT :lim"
            ),
            {"lim": min(limit, 500)},
        ).mappings().all()
    return {"eligibility": [dict(r) for r in rows]}


@router.get("/companies")
def list_companies() -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT c.id, c.canonical_name, c.watch_state, c.lifecycle_stage, "
                "c.first_seen_at, (SELECT count(*) FROM events e "
                "WHERE e.company_id = c.id) AS event_count, "
                "(SELECT r.state FROM eligibility_records r WHERE r.company_id = c.id "
                "ORDER BY r.detected_at DESC LIMIT 1) AS eligibility_state "
                "FROM companies c ORDER BY c.canonical_name"
            )
        ).mappings().all()
    return {"companies": [dict(r) for r in rows]}


@router.get("/companies/{company_id}/timeline")
def company_timeline(company_id: str) -> dict:
    """F-027: evidence-backed company history — original + updates (FR-DED-005)."""
    engine = get_engine()
    with engine.connect() as conn:
        company = conn.execute(
            sqlalchemy.text(
                "SELECT id, canonical_name, watch_state, lifecycle_stage, first_seen_at "
                "FROM companies WHERE id = CAST(:id AS uuid)"
            ),
            {"id": company_id},
        ).mappings().first()
        if company is None:
            raise HTTPException(status_code=404, detail="company not found")
        events = conn.execute(
            sqlalchemy.text(
                "SELECT e.id, e.type, e.status, e.title, e.deadline_at, e.start_at, "
                "e.current_payload, e.created_at, "
                "(SELECT count(*) FROM event_updates u WHERE u.event_id = e.id) "
                "AS update_count FROM events e WHERE e.company_id = CAST(:id AS uuid) "
                "ORDER BY e.created_at DESC"
            ),
            {"id": company_id},
        ).mappings().all()
        updates = conn.execute(
            sqlalchemy.text(
                "SELECT u.event_id, u.delta, u.detected_at FROM event_updates u "
                "JOIN events e ON e.id = u.event_id "
                "WHERE e.company_id = CAST(:id AS uuid) ORDER BY u.detected_at DESC"
            ),
            {"id": company_id},
        ).mappings().all()
        eligibility = conn.execute(
            sqlalchemy.text(
                "SELECT id, state, match_method, confidence, evidence, detected_at, "
                "correction_note FROM eligibility_records "
                "WHERE company_id = CAST(:id AS uuid) ORDER BY detected_at DESC"
            ),
            {"id": company_id},
        ).mappings().all()
        notifications = conn.execute(
            sqlalchemy.text(
                "SELECT n.id, n.priority, n.status, n.reason, n.created_at, n.sent_at "
                "FROM notifications n LEFT JOIN events e ON e.id = n.event_id "
                "WHERE e.company_id = CAST(:id AS uuid) "
                "ORDER BY n.created_at DESC LIMIT 50"
            ),
            {"id": company_id},
        ).mappings().all()
    timeline = [
        {"kind": "event", "at": str(e["created_at"]), **dict(e)} for e in events
    ] + [
        {"kind": "update", "at": str(u["detected_at"]), "event_id": str(u["event_id"]),
         "delta": u["delta"]} for u in updates
    ] + [
        {"kind": "eligibility", "at": str(x["detected_at"]), **dict(x)}
        for x in eligibility
    ] + [
        {"kind": "notification", "at": str(n["created_at"]), **dict(n)}
        for n in notifications
    ]
    timeline.sort(key=lambda item: item["at"], reverse=True)
    return {"company": dict(company), "timeline": timeline}


@router.get("/events")
def list_events(limit: int = 100) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT e.id, e.type, e.status, e.title, e.deadline_at, e.start_at, "
                "e.current_payload->>'venue' AS venue, e.created_at, "
                "c.canonical_name AS company FROM events e "
                "LEFT JOIN companies c ON c.id = e.company_id "
                "ORDER BY e.created_at DESC LIMIT :lim"
            ),
            {"lim": min(limit, 500)},
        ).mappings().all()
    return {"events": [dict(r) for r in rows]}


@router.get("/deadlines")
def list_deadlines(state: str | None = None, limit: int = 100) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT d.id, d.state, d.due_at, d.reminders_sent, e.type, "
                "c.canonical_name AS company FROM deadlines d "
                "JOIN events e ON e.id = d.event_id "
                "LEFT JOIN companies c ON c.id = e.company_id "
                "WHERE (CAST(:state AS deadline_state) IS NULL OR d.state = "
                "CAST(:state AS deadline_state)) ORDER BY d.due_at LIMIT :lim"
            ),
            {"state": state, "lim": min(limit, 500)},
        ).mappings().all()
    return {"deadlines": [dict(r) for r in rows]}


@router.get("/notifications")
def list_notifications(limit: int = 100) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT n.id, n.priority, n.status, n.reason, n.created_at, n.sent_at, "
                "n.payload->>'text' AS preview, "
                "(SELECT d.status FROM notification_deliveries d "
                "WHERE d.notification_id = n.id ORDER BY d.attempted_at DESC LIMIT 1) "
                "AS last_delivery FROM notifications n "
                "ORDER BY n.created_at DESC LIMIT :lim"
            ),
            {"lim": min(limit, 500)},
        ).mappings().all()
    return {"notifications": [dict(r) for r in rows]}


@router.get("/messages")
def list_messages(limit: int = 100, important_only: bool = True) -> dict:
    """Inbox (master §17): AI-filtered important messages with evidence."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT m.id, m.text, m.domain, m.importance, m.sent_at, "
                "m.processing_state, m.classification->>'status' AS dedup_status, "
                "g.name AS group_name FROM messages m "
                "JOIN groups g ON g.id = m.group_id "
                "WHERE m.text IS NOT NULL "
                "AND (:important_only = false OR (m.importance IN ('HIGH','CRITICAL') "
                "AND g.enabled)) "
                "ORDER BY m.sent_at DESC LIMIT :lim"
            ),
            {"important_only": important_only, "lim": min(limit, 500)},
        ).mappings().all()
    return {"messages": [dict(r) for r in rows]}


@router.get("/groups")
def list_groups() -> dict:
    """Settings screen data: allowlist config (FR-WA-004/005)."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT g.id, g.name, g.category, g.enabled, g.priority, "
                "(SELECT count(*) FROM messages m WHERE m.group_id = g.id) "
                "AS message_count FROM groups g ORDER BY g.enabled DESC, g.name"
            )
        ).mappings().all()
    return {"groups": [dict(r) for r in rows]}


@router.get("/audit")
def list_audit(limit: int = 200) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT a.actor, a.action, a.entity_type, a.entity_id, a.result, "
                "a.metadata, a.created_at FROM audit_logs a "
                "ORDER BY a.created_at DESC LIMIT :lim"
            ),
            {"lim": min(limit, 1000)},
        ).mappings().all()
    return {"audit": [dict(r) for r in rows]}


@router.get("/metrics")
def metrics() -> dict:
    """Operational counters (TRD §10.2, Redis-backed MVP form)."""
    import redis as redis_lib

    from pia_api.settings import get_settings

    settings = get_settings()
    counters: dict[str, int] = {}
    try:
        client = redis_lib.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
        for key in client.scan_iter("pia:metrics:*"):
            name = key.decode().removeprefix("pia:metrics:")
            counters[name] = int(client.get(key) or 0)
    except Exception:  # noqa: BLE001 — metrics are best-effort
        counters = {}
    return {"counters": dict(sorted(counters.items()))}
