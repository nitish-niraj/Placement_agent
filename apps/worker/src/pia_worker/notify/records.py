"""F-024 notification persistence — dedup anchor, delivery history, states.

The `UNIQUE(dedup_key)` constraint is the race-safe suppression anchor
(FR-NOT-004 / NFR-005): INSERT ... ON CONFLICT DO NOTHING returning no row
means this exact event state has already been notified. Delivery history
(notification_deliveries) records every attempt — sent AND failed.
"""

import json

import sqlalchemy
import structlog

from pia_shared.enums import NotificationPriority, NotificationStatus

logger = structlog.get_logger()


def get_user_id(conn: sqlalchemy.Connection) -> str:
    row = conn.execute(sqlalchemy.text("SELECT id FROM users LIMIT 1")).first()
    if row is None:
        raise RuntimeError("no user exists — cannot address notifications")
    return str(row.id)


def insert_notification(
    conn: sqlalchemy.Connection, *, dedup_key: str,
    priority: NotificationPriority, reason: str, payload: dict,
    evidence_refs: list[dict], event_id: str | None = None,
    eligibility_id: str | None = None, message_id: str | None = None,
    status: NotificationStatus = NotificationStatus.QUEUED,
) -> tuple[str, bool]:
    """Insert a notification; returns (id, is_new). is_new=False means the
    dedup anchor suppressed it — already delivered for this event state."""
    row = conn.execute(
        sqlalchemy.text(
            "INSERT INTO notifications (user_id, event_id, eligibility_id, "
            "message_id, priority, status, reason, dedup_key, payload, evidence_refs) "
            "VALUES (CAST(:user AS uuid), CAST(:event AS uuid), "
            "CAST(:elig AS uuid), CAST(:msg AS uuid), "
            "CAST(:priority AS notification_priority), "
            "CAST(:status AS notification_status), :reason, :dedup_key, "
            "CAST(:payload AS jsonb), CAST(:evidence AS jsonb)) "
            "ON CONFLICT (dedup_key) DO NOTHING RETURNING id"
        ),
        {
            "user": get_user_id(conn), "event": event_id, "elig": eligibility_id,
            "msg": message_id, "priority": priority.value,
            "status": status.value, "reason": reason,
            "dedup_key": dedup_key, "payload": json.dumps(payload, default=str),
            "evidence": json.dumps(evidence_refs, default=str),
        },
    ).first()
    if row is None:
        return "", False
    return str(row.id), True


def record_delivery(
    conn: sqlalchemy.Connection, notification_id: str, channel: str,
    ok: bool, error: str | None,
) -> None:
    conn.execute(
        sqlalchemy.text(
            "INSERT INTO notification_deliveries (notification_id, channel, status, "
            "error, attempted_at) VALUES (CAST(:nid AS uuid), :channel, "
            ":status, :error, now())"
        ),
        {"nid": notification_id, "channel": channel,
         "status": "sent" if ok else "failed", "error": error},
    )


def mark_sent(conn: sqlalchemy.Connection, notification_id: str) -> None:
    conn.execute(
        sqlalchemy.text(
            "UPDATE notifications SET status = CAST('SENT' AS notification_status), "
            "sent_at = now(), updated_at = now() WHERE id = CAST(:id AS uuid)"
        ),
        {"id": notification_id},
    )


def mark_pending_delivery(conn: sqlalchemy.Connection, notification_id: str,
                          error: str) -> None:
    """Backup path (TRD §8): Telegram failed N times — surface on the
    dashboard, never lost silently."""
    conn.execute(
        sqlalchemy.text(
            "UPDATE notifications SET status = "
            "CAST('PENDING_DELIVERY' AS notification_status), updated_at = now() "
            "WHERE id = CAST(:id AS uuid)"
        ),
        {"id": notification_id},
    )
    logger.warning("notification_pending_delivery", notification_id=notification_id,
                   error=error[:120])


def pending_digest_items(conn: sqlalchemy.Connection) -> list:
    """F-025: MEDIUM/LOW notifications waiting for the digest, oldest first."""
    rows = conn.execute(
        sqlalchemy.text(
            "SELECT n.id, n.priority, n.payload, n.reason, e.type AS event_type, "
            "c.canonical_name AS company FROM notifications n "
            "LEFT JOIN events e ON e.id = n.event_id "
            "LEFT JOIN companies c ON c.id = e.company_id "
            "WHERE n.status = 'PENDING' AND n.priority IN ('MEDIUM', 'LOW') "
            "ORDER BY n.created_at"
        )
    ).mappings().all()
    return list(rows)


def mark_digest_sent(conn: sqlalchemy.Connection, notification_ids: list[str],
                     digest_notification_id: str) -> None:
    for notification_id in notification_ids:
        conn.execute(
            sqlalchemy.text(
                "UPDATE notifications SET status = CAST('SENT' AS notification_status), "
                "sent_at = now(), updated_at = now() WHERE id = CAST(:id AS uuid)"
            ),
            {"id": notification_id},
        )
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO notification_deliveries (notification_id, channel, "
                "status, error, attempted_at) VALUES (CAST(:nid AS uuid), "
                "'telegram', 'sent', NULL, now())"
            ),
            {"nid": notification_id},
        )
    _ = digest_notification_id  # the digest message itself is logged, not stored


def load_notification(conn: sqlalchemy.Connection, notification_id: str) -> dict | None:
    row = conn.execute(
        sqlalchemy.text(
            "SELECT id, status, priority, payload, evidence_refs, dedup_key "
            "FROM notifications WHERE id = CAST(:id AS uuid)"
        ),
        {"id": notification_id},
    ).mappings().first()
    return dict(row) if row else None
