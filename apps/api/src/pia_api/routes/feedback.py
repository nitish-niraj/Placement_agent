"""Classification feedback intake — the corpus feedback loop.

- POST /api/v1/feedback/classification — record a correction for one
  message {message_id, correct_domain, correct_importance?, note?}.
  Stores predicted-vs-correct plus an audit row; never mutates the message
  or its downstream events (corrections inform future rules, they don't
  rewrite history).
- GET /api/v1/feedback/classification — intake queue for manual corpus
  curation (promotion to regression_messages.json stays a human decision).

Domain/importance values are validated against the frozen shared enums.
"""

import json

import sqlalchemy
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from pia_api.db import get_engine
from pia_api.deps import require_dashboard_token
from pia_shared.enums import Importance, MsgDomain

router = APIRouter(
    prefix="/api/v1", tags=["feedback"],
    dependencies=[Depends(require_dashboard_token)],
)


class ClassificationCorrection(BaseModel):
    message_id: str
    correct_domain: MsgDomain
    correct_importance: Importance | None = None
    note: str = ""


@router.post("/feedback/classification")
def submit_classification_feedback(body: ClassificationCorrection) -> dict:
    engine = get_engine()
    with engine.begin() as conn:
        message = conn.execute(
            sqlalchemy.text(
                "SELECT m.id, m.domain::text AS domain, "
                "m.importance::text AS importance, "
                "LEFT(m.text, 200) AS excerpt FROM messages m "
                "WHERE m.id = CAST(:id AS uuid)"
            ),
            {"id": body.message_id},
        ).mappings().first()
        if message is None:
            raise HTTPException(status_code=404,
                                detail="message not found")
        user_id = conn.execute(
            sqlalchemy.text(
                "SELECT id FROM users ORDER BY created_at LIMIT 1")
        ).scalar()
        row = conn.execute(
            sqlalchemy.text(
                "INSERT INTO classification_feedback (user_id, message_id, "
                "predicted_domain, predicted_importance, correct_domain, "
                "correct_importance, note) VALUES (:user, "
                "CAST(:message AS uuid), :pdom, :pimp, :cdom, :cimp, :note) "
                "RETURNING id, created_at"
            ),
            {"user": user_id, "message": body.message_id,
             "pdom": message["domain"] or "",
             "pimp": message["importance"] or "",
             "cdom": body.correct_domain.value,
             "cimp": body.correct_importance.value
             if body.correct_importance else "",
             "note": body.note[:500]},
        ).mappings().first()
        if row is None:  # pragma: no cover — RETURNING always yields a row
            raise HTTPException(status_code=409,
                                detail="feedback write lost its row; retry")
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO audit_logs (actor, action, entity_type, entity_id, "
                "result, metadata) VALUES ('user', 'classification.correction', "
                "'classification_feedback', CAST(:id AS uuid), 'ok', "
                "CAST(:meta AS jsonb))"
            ),
            {"id": str(row["id"]),
             "meta": json.dumps(
                 {"message_id": body.message_id,
                  "predicted": [message["domain"], message["importance"]],
                  "correct": [body.correct_domain.value,
                              body.correct_importance.value
                              if body.correct_importance else None]})},
        )
    return {"id": str(row["id"]), "message_id": body.message_id,
            "predicted_domain": message["domain"],
            "predicted_importance": message["importance"],
            "correct_domain": body.correct_domain.value,
            "correct_importance": body.correct_importance.value
            if body.correct_importance else None,
            "excerpt": message["excerpt"],
            "created_at": str(row["created_at"])}


@router.get("/feedback/classification")
def list_classification_feedback(
    limit: int = Query(default=50, le=200),
) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT f.id, f.message_id, f.predicted_domain, "
                "f.predicted_importance, f.correct_domain, "
                "f.correct_importance, f.note, f.created_at, "
                "LEFT(m.text, 160) AS excerpt FROM classification_feedback f "
                "JOIN messages m ON m.id = f.message_id "
                "ORDER BY f.created_at DESC LIMIT :limit"
            ),
            {"limit": limit},
        ).mappings().all()
    return {"feedback": [dict(r) for r in rows]}
