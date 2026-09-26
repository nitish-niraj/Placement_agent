"""Split-message bundling: one WhatsApp announcement can arrive as two rows.

Case A: image/excel first, announcement text as the next message.
Case B: text first, image/excel right below it.
Case C: image + caption in a single message (already merged by normalization).
Case D: a reply quoting an earlier message (reply_to_message_id edge).

All must evaluate as ONE entity: bundle_text + bundle_attachments.
Pure SQL helpers — no LLM, unit-testable with a fake connection.
"""

import sqlalchemy
import structlog

logger = structlog.get_logger()

DEFAULT_WINDOW_MINUTES = 10


def bundle_window_minutes() -> int:
    try:
        from pia_worker.settings import get_settings

        return int(get_settings().bundle_window_minutes)
    except Exception:  # noqa: BLE001 — tests without settings fall back
        return DEFAULT_WINDOW_MINUTES


def resolve_bundle_message_ids(
    conn: sqlalchemy.Connection, message_id: str | None,
    window_minutes: int | None = None,
) -> list[str]:
    """All message ids in the same group within ±window of the anchor.

    Falls back to [message_id] when the anchor is missing or the connection
    cannot answer (unit-test fakes, single-row fixtures).
    """
    if not message_id:
        return []
    try:
        anchor = conn.execute(
            sqlalchemy.text(
                "SELECT group_id, sent_at FROM messages "
                "WHERE id = CAST(:mid AS uuid)"
            ),
            {"mid": message_id},
        ).mappings().first()
    except Exception:  # noqa: BLE001 — fake connections in unit tests
        return [message_id]
    if anchor is None or anchor.get("group_id") is None:
        return [message_id]
    window = window_minutes if window_minutes is not None else bundle_window_minutes()
    try:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT id FROM messages "
                "WHERE group_id = :gid "
                "AND sent_at BETWEEN "
                "((SELECT sent_at FROM messages WHERE id = CAST(:mid AS uuid))"
                " - make_interval(mins => :window)) AND "
                "((SELECT sent_at FROM messages WHERE id = CAST(:mid AS uuid))"
                " + make_interval(mins => :window)) "
                "ORDER BY sent_at"
            ),
            {"gid": str(anchor["group_id"]), "mid": message_id, "window": window},
        ).mappings().all()
    except Exception:  # noqa: BLE001 — fallback keeps single-message behavior
        return [message_id]
    ids = [str(r["id"]) for r in rows if r.get("id") is not None]
    ids = ids or [message_id]
    # Case D: union the reply-thread parents (precise edge alongside the
    # time window — a reply quotes its parent even outside the window).
    for parent_id in resolve_thread_ids(conn, message_id):
        if parent_id not in ids:
            ids.append(parent_id)
    return ids


def resolve_thread_ids(
    conn: sqlalchemy.Connection, message_id: str | None, depth: int = 5,
) -> list[str]:
    """Reply-parent chain via reply_to_message_id (oldest last). Empty when
    the message is not a reply, the columns are absent (pre-migration), or
    the connection cannot answer (unit-test fakes)."""
    if not message_id:
        return []
    parents: list[str] = []
    seen = {message_id}
    current = message_id
    try:
        for _ in range(depth):
            row = conn.execute(
                sqlalchemy.text(
                    "SELECT reply_to_message_id FROM messages "
                    "WHERE id = CAST(:mid AS uuid)"
                ),
                {"mid": current},
            ).mappings().first()
            if row is None or not row.get("reply_to_message_id"):
                break
            parent = str(row["reply_to_message_id"])
            if parent in seen:
                break
            seen.add(parent)
            parents.append(parent)
            current = parent
    except Exception:  # noqa: BLE001 — fakes / pre-migration schema
        return []
    return parents


def bundle_text(conn: sqlalchemy.Connection, message_ids: list[str]) -> str:
    """Concatenated texts of a bundle (caption already merged at ingestion)."""
    if not message_ids:
        return ""
    try:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT text FROM messages WHERE id = ANY(:mids) ORDER BY sent_at"
            ),
            {"mids": message_ids},
        ).mappings().all()
    except Exception:  # noqa: BLE001 — fake connections
        return ""
    return "\n".join(str(r.get("text") or "") for r in rows if r.get("text")).strip()
