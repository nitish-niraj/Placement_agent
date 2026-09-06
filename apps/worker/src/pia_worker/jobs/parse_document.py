"""P5 job: parse a downloaded attachment into document_extractions (F-011..F-014).

Chained automatically after download_attachment succeeds. SEC-006: documents from
non-allowlisted groups are NOT parsed (storage only, visible reason).
"""

import json

import sqlalchemy
import structlog

from pia_shared.crypto import decrypt  # noqa: F401 — (SEC-002 read path reference)
from pia_worker.documents.orchestrator import parse_document_bytes
from pia_worker.jobs.process_message import PermanentJobError, _engine, _minio_client
from pia_worker.settings import get_settings

logger = structlog.get_logger()

MAX_RAW_TEXT = 100_000


def parse_document(attachment_id: str) -> str:
    settings = get_settings()
    engine = _engine()
    with engine.begin() as conn:
        row = conn.execute(
            sqlalchemy.text(
                "SELECT a.processing_state, a.mime_type, a.file_name, a.storage_key, "
                "g.enabled AS group_enabled, m.correlation_id "
                "FROM attachments a "
                "JOIN messages m ON m.id = a.message_id "
                "JOIN groups g ON g.id = m.group_id "
                "WHERE a.id = CAST(:id AS uuid)"
            ),
            {"id": attachment_id},
        ).first()
    if row is None:
        raise PermanentJobError(f"attachment {attachment_id} does not exist")
    if row.processing_state not in ("PROCESSED", "NEEDS_REVIEW"):
        raise PermanentJobError(
            f"attachment {attachment_id} not downloaded yet (state {row.processing_state})"
        )
    if not row.storage_key:
        raise PermanentJobError(f"attachment {attachment_id} has no stored object")
    if not row.group_enabled:
        logger.info("parse_skipped_not_allowlisted", attachment_id=attachment_id)
        return "skipped_not_allowlisted"

    client = _minio_client()
    response = client.get_object(settings.minio_bucket, row.storage_key)
    try:
        data = response.read()
    finally:
        response.close()
        response.release_conn()

    parsed = parse_document_bytes(
        mime_type=row.mime_type, file_name=row.file_name, data=data,
        correlation_id=row.correlation_id or "",
    )

    evidence = []
    if parsed.rows:
        evidence.append({"kind": "rows", "count": len(parsed.rows),
                         "sheets_or_pages": parsed.pages_or_sheets})
    payload = {
        "rows": [r.model_dump(mode="json") for r in parsed.rows],
        "detection": {"is_candidate_list": parsed.is_candidate_list,
                      "company": parsed.company},
        "notes": parsed.notes,
        "pages_or_sheets": parsed.pages_or_sheets,
    }
    with engine.begin() as conn:
        # Re-parses replace prior extractions for the same attachment (latest
        # wins). Match results referencing the old extractions are removed with
        # them (FK RESTRICT); P6 has no correction-preservation flow across
        # re-parses — the re-parse itself is the authoritative re-evaluation.
        conn.execute(
            sqlalchemy.text(
                "DELETE FROM eligibility_records WHERE source_document_id IN "
                "(SELECT id FROM document_extractions WHERE attachment_id = CAST(:aid AS uuid))"
            ),
            {"aid": attachment_id},
        )
        conn.execute(
            sqlalchemy.text(
                "DELETE FROM document_extractions WHERE attachment_id = CAST(:aid AS uuid)"
            ),
            {"aid": attachment_id},
        )
        extraction_id = str(conn.execute(
            sqlalchemy.text(
                "INSERT INTO document_extractions (attachment_id, extractor, "
                "extractor_version, raw_text, structured_payload, confidence, "
                "evidence, needs_review) "
                "VALUES (CAST(:aid AS uuid), :extractor, '1', :raw, "
                "CAST(:payload AS jsonb), :conf, CAST(:evidence AS jsonb), :review) "
                "RETURNING id"
            ),
            {
                "aid": attachment_id,
                "extractor": parsed.parser or "unknown",
                "raw": (parsed.text or "")[:MAX_RAW_TEXT],
                "payload": json.dumps(payload, default=str),
                "conf": parsed.confidence,
                "evidence": json.dumps(evidence),
                "review": parsed.needs_review,
            },
        ).scalar_one())
        if parsed.needs_review:
            conn.execute(
                sqlalchemy.text(
                    "UPDATE attachments SET processing_state = "
                    "CAST('NEEDS_REVIEW' AS attachment_state), updated_at = now() "
                    "WHERE id = CAST(:id AS uuid) AND processing_state = 'PROCESSED'"
                ),
                {"id": attachment_id},
            )
    # Chain the P6 eligibility engine on candidate lists — never blocks the
    # parse result; the matcher job owns its own retries (F-015/F-016).
    if parsed.is_candidate_list:
        try:
            from redis import Redis
            from rq import Queue

            from pia_worker.queue import DEFAULT_QUEUE

            Queue(DEFAULT_QUEUE, connection=Redis.from_url(settings.redis_url)).enqueue(
                "pia_worker.jobs.run_eligibility_match.run_eligibility_match", extraction_id
            )
        except Exception as exc:  # noqa: BLE001 — chaining failure is logged, retryable
            logger.warning("eligibility_enqueue_failed", extraction_id=extraction_id,
                           error=str(exc)[:120])
    logger.info(
        "document_parsed", attachment_id=attachment_id, extraction_id=extraction_id,
        parser=parsed.parser,
        rows=len(parsed.rows), is_candidate_list=parsed.is_candidate_list,
        company=parsed.company, needs_review=parsed.needs_review,
    )
    return f"parsed:{len(parsed.rows)}:rows"
