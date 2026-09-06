"""P2 job functions: message processing states, media download, retention.

- `process_message` (F-008): walks a message through QUEUED → PROCESSING → PROCESSED
  so every state of master §10.1 is queryable end-to-end. P2 "processing" is
  persistence verification; AI enrichment arrives in P3.
- `download_attachment` (FR-WA-006): downloads media via Evolution API, applies the
  size gate, uploads to MinIO with a SHA-256 checksum. Failed downloads are retryable
  jobs that land in the DLQ — never silent drops.
- `retention_cleanup` (SEC-007): deletes expired raw payloads. Normalized/derived
  facts (eligibility, events) are never auto-deleted (02_TRD §5).
"""

import datetime as dt
import hashlib
import io
import json
from typing import Any

import sqlalchemy
import structlog
from minio import Minio

from pia_shared.enums import AttachmentState, Importance, MessageState, MsgDomain
from pia_shared.media import MEDIA_KEYS
from pia_shared.states import assert_valid_transition
from pia_worker.settings import get_settings

logger = structlog.get_logger()


class PermanentJobError(RuntimeError):
    """Job can never succeed (bad reference, invalid source) — still dead-letters."""


def _engine() -> sqlalchemy.Engine:
    return sqlalchemy.create_engine(
        get_settings().database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3}
    )


def _set_message_state(conn: sqlalchemy.Connection, message_id: str, target: MessageState) -> None:
    conn.execute(
        sqlalchemy.text(
            "UPDATE messages SET processing_state = CAST(:s AS message_state), "
            "updated_at = now() WHERE id = :id"
        ),
        {"s": target.value, "id": message_id},
    )


# --- message processing (FR-MSG-004) ------------------------------------------


def process_message(message_id: str, correlation_id: str = "") -> str:
    """Validate -> queue -> process (classify, FR-CLS-001..003) -> processed."""
    engine = _engine()
    with engine.begin() as conn:
        row = conn.execute(
            sqlalchemy.text(
                "SELECT m.processing_state, m.text, g.category, g.enabled "
                "FROM messages m JOIN groups g ON g.id = m.group_id "
                "WHERE m.id = CAST(:id AS uuid)"
            ),
            {"id": message_id},
        ).first()
        if row is None:
            raise PermanentJobError(f"message {message_id} does not exist")

        current = MessageState(row.processing_state)
        if current is MessageState.PROCESSED:
            return "already"  # idempotent re-run after a retry

        for target in (MessageState.QUEUED, MessageState.PROCESSING):
            assert_valid_transition("message", current, target)
            _set_message_state(conn, message_id, target)
            current = target

        # --- SEC-006 gate: AI enrichment ONLY for allowlisted groups.
        classification_payload: dict[str, Any] = {
            "status": "skipped", "reason": "group_not_allowlisted",
        }
        domain: MsgDomain | None = None
        importance: Importance | None = None
        if row.enabled:
            # --- classification (P3): rules first, LLM second, rules win
            # (FR-CLS-004). Provider failure never fails the message: it is
            # persisted with a visible fallback marker (NFR-001 posture).
            classification_payload = {"status": "skipped"}
            try:
                from pia_worker.ai.classifier import classify_message, extract_entities

                result = classify_message(
                    text=row.text or "",
                    group_category=row.category,
                    correlation_id=correlation_id,
                )
                domain, importance = result.domain, result.importance
                classification_payload = result.model_dump(mode="json")

                # --- entity extraction (FR-CLS-003): skip chatter; links come
                # deterministic, companies/dates/actions via schema-validated LLM.
                if importance.value != "IGNORE" and domain.value not in ("GENERAL", "UNKNOWN"):
                    entities = extract_entities(
                        text=row.text or "", correlation_id=correlation_id,
                    )
                    classification_payload["entities"] = entities
                    # --- P7 company memory (FR-MEM-003/005): resolve mentions,
                    # persist linkage, bump priority for watched companies.
                    companies = entities.get("companies") or []
                    if companies:
                        importance = _link_companies(
                            conn, message_id, companies, classification_payload, importance
                        )
            except Exception as exc:  # noqa: BLE001 — enrichment must not block pipe
                classification_payload = {"status": "error", "error": str(exc)[:200]}
                logger.warning("classification_error", message_id=message_id,
                               error=str(exc)[:200])

        conn.execute(
            sqlalchemy.text(
                "UPDATE messages SET domain = CAST(:d AS msg_domain), "
                "importance = CAST(:i AS importance), classification = CAST(:c AS jsonb), "
                "updated_at = now() WHERE id = CAST(:id AS uuid)"
            ),
            {
                "d": domain.value if domain else None,
                "i": importance.value if importance else None,
                "c": json.dumps(classification_payload, default=str),
                "id": message_id,
            },
        )

        assert_valid_transition("message", MessageState.PROCESSING, MessageState.PROCESSED)
        _set_message_state(conn, message_id, MessageState.PROCESSED)

    # Chain the P8 event extractor for non-noise messages — deterministic, so
    # a cheap job with its own retries; never blocks the message result.
    if (
        row.enabled
        and importance is not None
        and importance.value != "IGNORE"
        and domain is not None
        and domain.value not in ("GENERAL", "UNKNOWN")
    ):
        _enqueue_extract_events(message_id)
    return "processed"


def _enqueue_extract_events(message_id: str) -> None:
    try:
        from redis import Redis
        from rq import Queue

        from pia_worker.queue import DEFAULT_QUEUE
        from pia_worker.settings import get_settings

        settings = get_settings()
        Queue(DEFAULT_QUEUE, connection=Redis.from_url(settings.redis_url)).enqueue(
            "pia_worker.jobs.extract_events.extract_events", message_id
        )
    except Exception as exc:  # noqa: BLE001 — chaining failure is logged, retryable
        logger.warning("extract_events_enqueue_failed", message_id=message_id,
                       error=str(exc)[:120])


# --- company memory (P7, FR-MEM-003/005) ---------------------------------------


def _link_companies(
    conn: sqlalchemy.Connection,
    message_id: str,
    company_mentions: list[dict],
    classification_payload: dict[str, Any],
    importance: Importance,
) -> Importance:
    """Resolve company mentions (F-017), persist the linkage as memory_records,
    and apply the watched-company priority bump (FR-MEM-005). Runs inside the
    message-processing transaction but behind the caller's failure isolation —
    company memory must never block message processing."""
    from pia_shared.enums import CompanyWatch
    from pia_worker.companies.mentions import associate_message_companies, watch_bump

    refs = associate_message_companies(
        conn, message_id, [str(m.get("name") or "") for m in company_mentions]
    )
    if not refs:
        return importance
    classification_payload["companies_resolved"] = [
        {"id": r.company_id, "canonical": r.canonical_name, "via": r.via}
        for r in refs
    ]
    watching = [r for r in refs if r.watch_state is CompanyWatch.WATCHING]
    if not watching:
        return importance
    bumped = watch_bump(importance)
    if bumped is None:
        return importance
    classification_payload["priority_bump"] = {
        "rule": "FR-MEM-005",
        "reason": "watched_company_mention",
        "companies": [r.canonical_name for r in watching],
        "from": importance.value,
        "to": bumped.value,
    }
    logger.info("watch_priority_bump", message_id=message_id,
                companies=[r.canonical_name for r in watching],
                importance=f"{importance.value}->{bumped.value}")
    return bumped


# --- attachments (FR-WA-006) ---------------------------------------------------


def _fetch_media_bytes(data: dict) -> bytes:
    """Media bytes come inline from the stored webhook payload: Evolution is
    configured with webhook base64=true, so the message content object carries a
    top-level `base64` field (sibling of imageMessage/documentMessage). No second
    network roundtrip to Evolution is needed."""
    message = data.get("message") or {}
    if message.get("base64"):  # injected by Evolution: message.base64 (v2.3.x)
        return _decode_base64(str(message["base64"]))
    for key in MEDIA_KEYS:  # defensive: some versions nest it inside the media object
        media = message.get(key)
        if key == "documentWithCaptionMessage":
            media = (message.get("documentWithCaptionMessage") or {}).get(
                "message", {}
            ).get("documentMessage")
        if isinstance(media, dict) and media.get("base64"):
            return _decode_base64(str(media["base64"]))
    if data.get("base64"):
        return _decode_base64(str(data["base64"]))
    raise PermanentJobError(
        "media content missing in stored payload (webhook base64 enabled after "
        "this message arrived)"
    )


def _decode_base64(value: str) -> bytes:
    import base64

    return base64.b64decode(value.split(",", 1)[-1])


def _minio_client() -> Minio:
    settings = get_settings()
    endpoint = settings.minio_endpoint
    secure = endpoint.startswith("https://")
    host = endpoint.removeprefix("https://").removeprefix("http://")
    return Minio(host, access_key=settings.minio_access_key,
                 secret_key=settings.minio_secret_key, secure=secure)


def download_attachment(attachment_id: str) -> str:
    settings = get_settings()
    engine = _engine()

    with engine.begin() as conn:
        row = conn.execute(
            sqlalchemy.text(
                "SELECT processing_state, message_id, mime_type, file_name "
                "FROM attachments WHERE id = CAST(:id AS uuid)"
            ),
            {"id": attachment_id},
        ).first()
        if row is None:
            raise PermanentJobError(f"attachment {attachment_id} does not exist")
        if AttachmentState(row.processing_state) is AttachmentState.PROCESSED:
            return "already"
        message_id = str(row.message_id)
        mime = row.mime_type
        file_name = row.file_name
        conn.execute(
            sqlalchemy.text(
                "UPDATE attachments SET processing_state = "
                "CAST('DOWNLOADING' AS attachment_state), "
                "updated_at = now() WHERE id = CAST(:id AS uuid)"
            ),
            {"id": attachment_id},
        )

    raw = engine.connect()
    with raw:
        payload_row = raw.execute(
            sqlalchemy.text(
                "SELECT payload FROM raw_event_payloads WHERE message_id = CAST(:id AS uuid) "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"id": message_id},
        ).first()
    if payload_row is None:
        raise PermanentJobError(f"no raw payload retained for message {message_id}")
    data = payload_row.payload.get("data") if isinstance(payload_row.payload, dict) else None
    if not isinstance(data, dict):
        raise PermanentJobError(f"raw payload for message {message_id} has no data object")

    content = _fetch_media_bytes(data)

    max_bytes = settings.media_max_size_mb * 1024 * 1024
    if len(content) > max_bytes:
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "UPDATE attachments SET processing_state = "
                    "CAST('REJECTED_OVERSIZE' AS attachment_state), "
                    "failure_reason = :reason, updated_at = now() WHERE id = CAST(:id AS uuid)"
                ),
                {"id": attachment_id, "reason": f"{len(content)} bytes > {max_bytes} limit"},
            )
        logger.warning("attachment_rejected_oversize", attachment_id=attachment_id)
        return "rejected_oversize"

    checksum = hashlib.sha256(content).hexdigest()
    storage_key = f"attachments/{message_id}/{attachment_id}/{file_name or 'media'}"
    client = _minio_client()
    if not client.bucket_exists(settings.minio_bucket):
        client.make_bucket(settings.minio_bucket)
    client.put_object(
        settings.minio_bucket, storage_key, io.BytesIO(content), len(content),
        content_type=mime,
    )

    with engine.begin() as conn:
        conn.execute(
            sqlalchemy.text(
                "UPDATE attachments SET storage_key = :key, checksum = :checksum, "
                "size_bytes = :size, processing_state = CAST('PROCESSED' AS attachment_state), "
                "updated_at = now() WHERE id = CAST(:id AS uuid)"
            ),
            {
                "key": storage_key,
                "checksum": checksum,
                "size": len(content),
                "id": attachment_id,
            },
        )
    # Chain the P5 parser (document_extractions) — never blocks the download result
    try:
        from redis import Redis
        from rq import Queue

        from pia_worker.queue import DEFAULT_QUEUE

        Queue(DEFAULT_QUEUE, connection=Redis.from_url(settings.redis_url)).enqueue(
            "pia_worker.jobs.parse_document.parse_document", attachment_id
        )
    except Exception as exc:  # noqa: BLE001 — chaining failure is logged, retryable
        logger.warning("parse_enqueue_failed", attachment_id=attachment_id,
                       error=str(exc)[:120])
    logger.info(
        "attachment_stored", attachment_id=attachment_id, size=len(content), key=storage_key
    )
    return "stored"


# --- retention (SEC-007) --------------------------------------------------------


def retention_cleanup() -> dict[str, int]:
    settings = get_settings()
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=settings.raw_message_retention_days)
    engine = _engine()
    with engine.begin() as conn:
        result = conn.execute(
            sqlalchemy.text("DELETE FROM raw_event_payloads WHERE retention_expires_at < :c"),
            {"c": cutoff},
        )
        deleted = result.rowcount or 0
    logger.info("retention_cleanup_done", deleted_raw_payloads=deleted, cutoff=cutoff.isoformat())
    return {"deleted_raw_payloads": deleted}
