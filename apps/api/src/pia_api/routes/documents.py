"""Documents API (additive to master §16): parsed candidate lists + evidence."""

import sqlalchemy
from fastapi import APIRouter, Depends, HTTPException

from pia_api.db import get_engine
from pia_api.deps import require_dashboard_token

router = APIRouter(prefix="/api/v1", tags=["documents"],
                   dependencies=[Depends(require_dashboard_token)])


@router.get("/documents")
def list_documents(limit: int = 50, needs_review: bool | None = None) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT d.id, d.extractor, d.confidence, d.needs_review, "
                "d.structured_payload->'detection' AS detection, "
                "jsonb_array_length(COALESCE(d.structured_payload->'rows', '[]'::jsonb)) "
                "AS row_count, "
                "a.file_name, a.mime_type, a.processing_state AS attachment_state, "
                "g.name AS group_name, m.sent_at "
                "FROM document_extractions d "
                "JOIN attachments a ON a.id = d.attachment_id "
                "JOIN messages m ON m.id = a.message_id "
                "JOIN groups g ON g.id = m.group_id "
                "WHERE (CAST(:needs_review AS boolean) IS NULL "
                "OR d.needs_review = :needs_review) "
                "ORDER BY d.created_at DESC LIMIT :limit"
            ),
            {"needs_review": needs_review, "limit": min(limit, 200)},
        ).mappings().all()
    return {"data": [dict(r) for r in rows]}


@router.get("/documents/{extraction_id}")
def get_document(extraction_id: str) -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            sqlalchemy.text(
                "SELECT d.id, d.extractor, d.extractor_version, d.raw_text, "
                "d.structured_payload, d.confidence, d.evidence, d.needs_review, "
                "a.file_name, a.mime_type, g.name AS group_name, m.sent_at "
                "FROM document_extractions d "
                "JOIN attachments a ON a.id = d.attachment_id "
                "JOIN messages m ON m.id = a.message_id "
                "JOIN groups g ON g.id = m.group_id "
                "WHERE d.id = CAST(:id AS uuid)"
            ),
            {"id": extraction_id},
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="extraction not found")
    return {"data": dict(row)}
