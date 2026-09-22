"""Application-state API — did the student actually apply?

- GET  /api/v1/applications[?company=] — tracked opportunities (company+role,
  status, applied_at) with company names.
- POST /api/v1/applications/answer — record the student's answer
  {company, role?, status, note?}. Company matches by id or name; the role is
  normalized server-side. Every change is machine-asserted and audited, same
  posture as POST /feedback/match and the actions approve/reject endpoints.

Company mentioned != applied: these rows are the source of truth the
notification gates consult before any post-application follow-up.
"""

import json

import sqlalchemy
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from pia_api.db import get_engine
from pia_api.deps import require_dashboard_token
from pia_shared.enums import ApplicationStatus
from pia_shared.states import InvalidTransitionError, assert_valid_transition
from pia_shared.textnorm import normalize_name

router = APIRouter(
    prefix="/api/v1", tags=["applications"],
    dependencies=[Depends(require_dashboard_token)],
)


class AnswerBody(BaseModel):
    company: str  # company id (uuid) or canonical name / alias
    role: str | None = None
    status: ApplicationStatus
    note: str = ""


def _resolve_company(conn: sqlalchemy.Connection, company: str) -> dict:
    """Company id by uuid or by (normalized) name/alias. 404 when unknown."""
    needle = (company or "").strip()
    if not needle:
        raise HTTPException(status_code=422, detail="company is required")
    row = conn.execute(
        sqlalchemy.text(
            "SELECT c.id, c.canonical_name, c.normalized_key FROM companies c "
            "LEFT JOIN company_aliases a ON a.company_id = c.id "
            "WHERE c.id = CAST(NULLIF(:needle, '') AS uuid) "
            "OR c.normalized_key = :norm OR a.alias = :norm LIMIT 1"
        ),
        {"needle": needle if len(needle) == 36 else "",
         "norm": normalize_name(needle)},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"unknown company: {needle[:60]}")
    return dict(row)


@router.get("/applications")
def list_applications(
    company: str | None = Query(default=None),
) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        sql = (
            "SELECT s.id, c.canonical_name AS company, s.role_normalized AS role, "
            "s.opportunity_key, s.status, s.applied_at, s.source, s.note, "
            "s.updated_at FROM application_states s "
            "JOIN companies c ON c.id = s.company_id "
            "JOIN users u ON u.id = s.user_id "
            "WHERE u.id = (SELECT id FROM users ORDER BY created_at LIMIT 1)"
        )
        params: dict[str, object] = {}
        if company:
            sql += " AND c.normalized_key = :norm"
            params["norm"] = normalize_name(company)
        sql += " ORDER BY s.updated_at DESC LIMIT 200"
        rows = [dict(r) for r in
                conn.execute(sqlalchemy.text(sql), params).mappings().all()]
    return {"applications": rows}


@router.post("/applications/answer")
def answer_application(body: AnswerBody) -> dict:
    engine = get_engine()
    role_norm = normalize_name(body.role)
    with engine.begin() as conn:
        company = _resolve_company(conn, body.company)
        user_id = conn.execute(
            sqlalchemy.text(
                "SELECT id FROM users ORDER BY created_at LIMIT 1")
        ).scalar()
        current = conn.execute(
            sqlalchemy.text(
                "SELECT id, status FROM application_states "
                "WHERE user_id = :user AND company_id = CAST(:company AS uuid) "
                "AND role_normalized = :role"
            ),
            {"user": user_id, "company": str(company["id"]),
             "role": role_norm},
        ).mappings().first()
        if current is None:
            key = f"{company['normalized_key']}|{role_norm}"
            current = conn.execute(
                sqlalchemy.text(
                    "INSERT INTO application_states (user_id, company_id, "
                    "role_normalized, opportunity_key, status, source) "
                    "VALUES (:user, CAST(:company AS uuid), :role, :key, "
                    "'UNKNOWN', 'dashboard') "
                    "ON CONFLICT (user_id, company_id, role_normalized) "
                    "DO NOTHING RETURNING id, status"
                ),
                {"user": user_id, "company": str(company["id"]),
                 "role": role_norm, "key": key},
            ).mappings().first()
            if current is None:  # pragma: no cover — raced creation
                raise HTTPException(status_code=409,
                                    detail="answer raced another write; retry")
        try:
            assert_valid_transition(
                "application", ApplicationStatus(current["status"]),
                body.status)
        except InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        updated = conn.execute(
            sqlalchemy.text(
                "UPDATE application_states SET status = :status, applied_at = "
                "CASE WHEN :status = 'APPLIED' THEN COALESCE(applied_at, now()) "
                "ELSE applied_at END, source = 'dashboard', note = :note, "
                "updated_at = now() WHERE id = CAST(:id AS uuid) "
                "RETURNING id, status, applied_at, source, note, "
                "opportunity_key, updated_at"
            ),
            {"status": body.status.value, "note": body.note[:500],
             "id": str(current["id"])},
        ).mappings().first()
        if updated is None:  # pragma: no cover — row was just written above
            raise HTTPException(status_code=409,
                                detail="answer write lost its row; retry")
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO audit_logs (actor, action, entity_type, entity_id, "
                "result, metadata) VALUES ('user', 'application.status_change', "
                "'application_states', CAST(:id AS uuid), 'ok', "
                "CAST(:meta AS jsonb))"
            ),
            {"id": str(current["id"]),
             "meta": json.dumps({"from": current["status"],
                                 "to": body.status.value,
                                 "via": "dashboard"})},
        )
    result = dict(updated)
    result["company"] = company["canonical_name"]
    result["role"] = role_norm
    return result
