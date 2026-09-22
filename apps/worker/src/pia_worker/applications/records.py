"""Application-state persistence (company+role opportunity).

Read side for the notification gates; write side for Telegram buttons,
the dashboard API, and the eligibility hook (ELIGIBLE -> ELIGIBLE_NOT_APPLIED).
Every status change is asserted on the "application" machine and audited —
the pipeline itself never writes APPLIED (only the student's explicit answer
does that).
"""

import json

import sqlalchemy
import structlog

from pia_shared.enums import ApplicationStatus
from pia_shared.states import assert_valid_transition
from pia_shared.textnorm import normalize_name

logger = structlog.get_logger()

# Eligibility-equivalent states on the list-membership side.
ELIGIBLE_RECORD_STATES = ("ELIGIBLE", "USER_CONFIRMED")


def normalize_role(role: str | None) -> str:
    """Role key form: same normalization as company keys (FR-ELG-005)."""
    return normalize_name(role)


def opportunity_key(company_normalized: str, role_normalized: str) -> str:
    """Stable per-opportunity key for Telegram callbacks and dedup."""
    return f"{company_normalized or ''}|{role_normalized or ''}"


def get_application(
    conn: sqlalchemy.Connection, user_id: str, company_id: str,
    role_normalized: str = "",
) -> dict | None:
    row = conn.execute(
        sqlalchemy.text(
            "SELECT id, status, applied_at, source, note, opportunity_key, "
            "updated_at FROM application_states "
            "WHERE user_id = CAST(:user AS uuid) "
            "AND company_id = CAST(:company AS uuid) "
            "AND role_normalized = :role"
        ),
        {"user": user_id, "company": company_id,
         "role": role_normalized or ""},
    ).mappings().first()
    return dict(row) if row else None


def ensure_application(
    conn: sqlalchemy.Connection, *, user_id: str, company_id: str,
    company_normalized: str = "", role_normalized: str = "",
    source: str, initial: ApplicationStatus = ApplicationStatus.UNKNOWN,
) -> dict:
    """Get-or-create the opportunity row. Creation only ever uses UNKNOWN or
    ELIGIBLE_NOT_APPLIED — never an applied-side state."""
    if initial not in (ApplicationStatus.UNKNOWN,
                       ApplicationStatus.ELIGIBLE_NOT_APPLIED):
        raise ValueError(f"pipeline may not create {initial!r} rows")
    existing = get_application(conn, user_id, company_id, role_normalized)
    if existing is not None:
        return existing
    key = opportunity_key(company_normalized, role_normalized)
    inserted = conn.execute(
        sqlalchemy.text(
            "INSERT INTO application_states (user_id, company_id, role_normalized, "
            "opportunity_key, status, source) VALUES (CAST(:user AS uuid), "
            "CAST(:company AS uuid), :role, :key, CAST(:status AS text), :source) "
            "ON CONFLICT (user_id, company_id, role_normalized) DO NOTHING "
            "RETURNING id, status, applied_at, source, note, opportunity_key, "
            "updated_at"
        ),
        {"user": user_id, "company": company_id, "role": role_normalized or "",
         "key": key, "status": initial.value, "source": source},
    ).mappings().first()
    if not inserted:  # lost a race with another writer — read the winner
        existing = get_application(conn, user_id, company_id, role_normalized)
        if existing is None:  # pragma: no cover — defensive
            raise RuntimeError("application_states upsert lost without a winner")
        return existing
    row = dict(inserted)
    conn.execute(
        sqlalchemy.text(
            "INSERT INTO audit_logs (actor, action, entity_type, entity_id, "
            "result, metadata) VALUES (:actor, 'application.created', "
            "'application_states', CAST(:id AS uuid), 'ok', "
            "CAST(:meta AS jsonb))"
        ),
        {"actor": source, "id": str(row["id"]),
         "meta": json.dumps({"status": initial.value, "key": key})},
    )
    return dict(row)


def set_application_status(
    conn: sqlalchemy.Connection, *, user_id: str, company_id: str,
    role_normalized: str = "", status: ApplicationStatus,
    source: str, note: str = "",
) -> dict:
    """Record the student's explicit answer. Any -> any (user-driven machine);
    applied_at is stamped exactly when the state becomes APPLIED."""
    current = get_application(conn, user_id, company_id, role_normalized)
    if current is None:
        current = ensure_application(
            conn, user_id=user_id, company_id=company_id,
            role_normalized=role_normalized, source=source)
    assert_valid_transition(
        "application", ApplicationStatus(current["status"]), status)
    updated = conn.execute(
        sqlalchemy.text(
            "UPDATE application_states SET status = CAST(:status AS text), "
            "applied_at = CASE WHEN CAST(:status AS text) = 'APPLIED' "
            "THEN COALESCE(applied_at, now()) ELSE applied_at END, "
            "source = :source, note = :note, updated_at = now() "
            "WHERE user_id = CAST(:user AS uuid) "
            "AND company_id = CAST(:company AS uuid) "
            "AND role_normalized = :role "
            "RETURNING id, status, applied_at, source, note, opportunity_key, "
            "updated_at"
        ),
        {"user": user_id, "company": company_id, "role": role_normalized or "",
         "status": status.value, "source": source, "note": note[:500]},
    ).mappings().first()
    if not updated:  # pragma: no cover — row was just ensured above
        raise RuntimeError("application_states update affected no row")
    row = dict(updated)
    conn.execute(
        sqlalchemy.text(
            "INSERT INTO audit_logs (actor, action, entity_type, entity_id, "
            "result, metadata) VALUES (:actor, 'application.status_change', "
            "'application_states', CAST(:id AS uuid), 'ok', "
            "CAST(:meta AS jsonb))"
        ),
        {"actor": source, "id": str(row["id"]),
         "meta": json.dumps({"from": current["status"], "to": status.value,
                             "note": note[:200]})},
    )
    logger.info("application_status_set", opportunity=row["opportunity_key"],
                status=status.value, source=source)
    return dict(row)


def resolve_match(
    conn: sqlalchemy.Connection, user_id: str, company_id: str | None,
    role_normalized: str = "",
) -> tuple[dict | None, bool]:
    """Match a message's (company, role) to a tracked opportunity.

    Returns (row, matched). Exact (company, role) rows win. A role-less
    message falls back to any APPLIED row for the company — a role-less
    announcement cannot name a role, so any applied role of that company
    counts; a message naming an untracked role never matches.
    """
    if not company_id:
        return None, False
    exact = get_application(conn, user_id, company_id, role_normalized)
    if exact is not None:
        return exact, True
    if not role_normalized:
        row = conn.execute(
            sqlalchemy.text(
                "SELECT id, status, applied_at, source, note, opportunity_key, "
                "updated_at FROM application_states "
                "WHERE user_id = CAST(:user AS uuid) "
                "AND company_id = CAST(:company AS uuid) "
                "AND status = 'APPLIED' ORDER BY updated_at DESC LIMIT 1"
            ),
            {"user": user_id, "company": company_id},
        ).mappings().first()
        if row is not None:
            return dict(row), True
    return None, False


def list_applications_for_company(
    conn: sqlalchemy.Connection, user_id: str, company_id: str,
) -> list[dict]:
    """All opportunity rows for one company — reviewer context + UX listing."""
    rows = conn.execute(
        sqlalchemy.text(
            "SELECT id, role_normalized, opportunity_key, status, applied_at, "
            "source, note, updated_at FROM application_states "
            "WHERE user_id = CAST(:user AS uuid) "
            "AND company_id = CAST(:company AS uuid) "
            "ORDER BY updated_at DESC"
        ),
        {"user": user_id, "company": company_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def message_list_gate(
    conn: sqlalchemy.Connection, message_id: str | None,
    company_id: str | None,
) -> str:
    """Strict CSV/image gate for one message: 'eligible' | 'not_found' | 'no_list'.

    'not_found' = the message carries a candidate-list attachment (CSV, XLSX,
    PDF, or OCR'd image) and no ELIGIBLE/USER_CONFIRMED eligibility record
    ties this message+company to the student. Callers suppress FORM/KYC/
    REGISTRATION notifications, drafts, and reminders on 'not_found'.
    'no_list' = no candidate-list attachment — text-only path, unchanged.
    """
    if not message_id:
        return "no_list"
    payloads = conn.execute(
        sqlalchemy.text(
            "SELECT d.structured_payload FROM document_extractions d "
            "JOIN attachments a ON a.id = d.attachment_id "
            "WHERE a.message_id = CAST(:msg AS uuid)"
        ),
        {"msg": message_id},
    ).mappings().all()
    has_list = any(
        ((r["structured_payload"] or {}).get("detection") or {}).get(
            "is_candidate_list")
        for r in payloads
    )
    if not has_list:
        return "no_list"
    if company_id:
        hit = conn.execute(
            sqlalchemy.text(
                "SELECT 1 FROM eligibility_records "
                "WHERE source_message_id = CAST(:msg AS uuid) "
                "AND company_id = CAST(:company AS uuid) "
                "AND state IN ('ELIGIBLE', 'USER_CONFIRMED') LIMIT 1"
            ),
            {"msg": message_id, "company": company_id},
        ).first()
        if hit is not None:
            return "eligible"
        return "not_found"
    any_hit = conn.execute(
        sqlalchemy.text(
            "SELECT 1 FROM eligibility_records "
            "WHERE source_message_id = CAST(:msg AS uuid) "
            "AND state IN ('ELIGIBLE', 'USER_CONFIRMED') LIMIT 1"
        ),
        {"msg": message_id},
    ).first()
    return "eligible" if any_hit is not None else "not_found"
