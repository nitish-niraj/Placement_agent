"""Evolution API webhook receiver (F-005, P1).

Security posture (SEC-003): fail-closed shared-secret header (`X-PIA-Token`,
constant-time compare) + per-IP rate limit via Redis. Raw payloads are persisted
for audit under retention (FR-MSG-003); messages are normalized to the canonical
schema (FR-MSG-001) with duplicate deliveries collapsed (FR-MSG-002 anchor).

Deliberately minimal for P1: persistence + connection state + group metadata.
Classification/enrichment jobs arrive in P2/P3.
"""

import base64
import datetime as dt
import hmac
import re
import uuid
from typing import Any

import redis as redis_lib
import structlog
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from pia_api.db import get_engine
from pia_api.normalization import (
    content_hash,
    extract_text,
    map_connection_state,
    parse_timestamp,
)
from pia_api.settings import get_settings
from pia_shared.dedup import (
    DedupVerdict,
    decide_exact_duplicate,
    decide_near_duplicate,
    decide_visual_duplicate,
    token_jaccard,
)
from pia_shared.enums import MessageState
from pia_shared.media import MEDIA_KEYS, guess_media
from pia_shared.metrics import increment
from pia_shared.phash import dhash, hamming_hex
from pia_shared.states import assert_valid_transition

logger = structlog.get_logger()
router = APIRouter(tags=["webhooks"])

RATE_LIMIT_WINDOW_SECONDS = 60


def _rate_limited(ip: str) -> bool:
    settings = get_settings()
    try:
        client = redis_lib.Redis.from_url(settings.redis_url, socket_connect_timeout=1)
        key = f"pia:rl:webhook:{ip}"
        count = client.incr(key)
        if count == 1:
            client.expire(key, RATE_LIMIT_WINDOW_SECONDS)
        return count > settings.webhook_rate_limit_per_minute
    except Exception:  # noqa: BLE001 — limiter outage fails OPEN: auth (SEC-003) is
        logger.warning("rate_limiter_unavailable")  # still enforced; flow never dies
        return False


def _authorized(token_header: str | None) -> bool:
    secret = get_settings().evolution_webhook_secret
    if not secret:
        return False  # fail-closed (SEC-003)
    provided = token_header or ""
    return hmac.compare_digest(provided, secret)


@router.post("/webhooks/evolution")
async def evolution_webhook(
    request: Request,
    x_pia_token: str | None = Header(default=None, alias="X-PIA-Token"),
) -> JSONResponse:
    # Handlers below are sync DB calls executed on the event loop — fine at
    # single-user volume; move to run_in_threadpool if latency ever demands it.
    ip = request.client.host if request.client else "unknown"
    if not _authorized(x_pia_token):
        logger.warning("webhook_rejected", reason="auth", source_ip=ip)
        return JSONResponse(status_code=403, content={"error": {"code": "FORBIDDEN"}})
    if _rate_limited(ip):
        logger.warning("webhook_rejected", reason="rate_limit", source_ip=ip)
        return JSONResponse(status_code=429, content={"error": {"code": "RATE_LIMITED"}})

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_ERROR"}})

    event = str(payload.get("event", ""))
    instance = str(payload.get("instance", "unknown"))
    data = payload.get("data")

    correlation_id = str(uuid.uuid4())
    stored = duplicates = ignored = 0
    to_enqueue: list[tuple[str, str | None]] = []
    try:
        if event == "connection.update":
            _handle_connection_update(instance, data if isinstance(data, dict) else {})
        elif event == "groups.upsert":
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict):
                    ignored += _handle_group_upsert(item)
        elif event == "messages.upsert":
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict):
                    result, message_id, attachment_id = _handle_message_upsert(
                        instance, item, correlation_id, payload
                    )
                    if result == "stored":
                        stored += 1
                        assert message_id is not None  # guaranteed on "stored"
                        to_enqueue.append((message_id, attachment_id))
                    elif result == "duplicate":
                        duplicates += 1
                    else:
                        ignored += 1
        else:
            ignored += 1  # known-but-unhandled events stay visible in logs below
    except Exception as exc:  # visible failure, never silent (NFR-001)
        logger.error("webhook_processing_failed", event=event, error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "PROCESSING_FAILED", "request_id": correlation_id}},
        )

    # Job fan-out happens after the DB transaction committed. An enqueue failure
    # must not fail the webhook — the message stays VALIDATED (visible, re-driveable).
    from pia_api.jobs import enqueue_download_attachment, enqueue_process_message

    for message_id, attachment_id in to_enqueue:
        try:
            enqueue_process_message(message_id, correlation_id)
            if attachment_id:
                enqueue_download_attachment(attachment_id)
        except Exception as exc:
            logger.error("enqueue_failed", message_id=message_id, error=str(exc))

    logger.info(
        "webhook_accepted",
        event_name=event,
        instance=instance,
        stored=stored,
        duplicates=duplicates,
        ignored=ignored,
        correlation_id=correlation_id,
    )
    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "stored": stored,
            "duplicates": duplicates,
            "ignored": ignored,
            "request_id": correlation_id,
        },
    )


# --- handlers -----------------------------------------------------------------


def _handle_connection_update(instance: str, data: dict) -> None:
    status = map_connection_state(data.get("state"))
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO whatsapp_instances "
                "(instance_key, provider, status, secret_ref, last_seen_at) "
                "VALUES (:k, 'evolution', CAST(:s AS instance_status), "
                "'env:EVOLUTION_API_KEY', now()) "
                "ON CONFLICT (instance_key) DO UPDATE "
                "SET status = EXCLUDED.status, last_seen_at = now(), updated_at = now()"
            ),
            {"k": instance, "s": status.value},
        )
    logger.info("instance_status_persisted", instance=instance, status=status.value)


def _handle_group_upsert(item: dict) -> int:
    group_id = item.get("id")
    if not group_id:
        return 1
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO groups (provider_group_id, name, participant_meta) "
                "VALUES (:gid, :name, CAST(:meta AS jsonb)) "
                "ON CONFLICT (provider_group_id) DO UPDATE "
                "SET name = EXCLUDED.name, participant_meta = EXCLUDED.participant_meta, "
                "    updated_at = now()"
                # enabled/category are owner decisions — never overwritten here (FR-WA-004/005)
            ),
            {"gid": group_id, "name": item.get("subject") or group_id, "meta": _json(item)},
        )
    return 0


def _handle_message_upsert(
    instance: str, item: dict, correlation_id: str, raw: dict
) -> tuple[str, str | None, str | None]:
    key = item.get("key") or {}
    provider_message_id = key.get("id")
    jid = key.get("remoteJid")
    if not provider_message_id or not jid:
        return "ignored", None, None  # missing identity fields (FR-MSG-001 contract)

    message_body = item.get("message") or {}
    text_value = extract_text(message_body)
    message_type = str(item.get("messageType") or "unknown")
    sent_at = parse_timestamp(item.get("messageTimestamp"))
    settings = get_settings()

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO groups (provider_group_id, name, category) "
                "VALUES (:gid, :name, CAST('OTHER' AS group_category)) "
                "ON CONFLICT (provider_group_id) DO NOTHING"
            ),
            {"gid": jid, "name": jid},
        )
        group_row = conn.execute(
            text("SELECT id FROM groups WHERE provider_group_id = :gid"), {"gid": jid}
        ).one()

        # P9 visual layer input: dHash of image media at ingestion (DEC-009).
        media = guess_media(item)
        phash_hex = None
        if media and media[0].startswith("image/"):
            image_bytes = _image_bytes(item)
            if image_bytes is not None:
                phash_hex = dhash(image_bytes)

        chash_value = content_hash(text_value, message_type)

        inserted = conn.execute(
            text(
                "INSERT INTO messages (provider_message_id, group_id, sender_id, "
                "sender_name, sent_at, text, content_hash, image_phash, "
                "processing_state, correlation_id, retention_expires_at) "
                "VALUES (:pid, :gid, :sender, :sender_name, :sent_at, :txt, :chash, "
                ":phash, CAST('RECEIVED' AS message_state), :corr, :retention) "
                "ON CONFLICT (provider_message_id, group_id) DO NOTHING RETURNING id"
            ),
            {
                "pid": provider_message_id,
                "gid": group_row.id,
                "sender": key.get("participant") or key.get("remoteJid") or "unknown",
                "sender_name": item.get("pushName"),
                "sent_at": sent_at,
                "txt": text_value,
                "chash": chash_value,
                "phash": phash_hex,
                "corr": correlation_id,
                "retention": dt.datetime.now(dt.UTC)
                + dt.timedelta(days=settings.raw_message_retention_days),
            },
        ).first()

        if inserted is None:
            return "duplicate", None, None  # FR-MSG-002: same event -> one logical message

        # P9 dedup cascade (exact -> visual -> near) BEFORE any enrichment
        # fan-out: suppressed copies are persisted + audited but never
        # classified, never parsed, never notified (FR-DED-001..003).
        verdict, of_message_id = _dedup_verdict(
            conn, str(inserted.id), str(group_row.id), text_value, chash_value, phash_hex
        )
        if verdict.suppress:
            _mark_suppressed(conn, str(inserted.id), verdict, of_message_id)
            return "duplicate", None, None

        # Media present -> register an attachment row (FR-WA-006); the worker
        # downloads it via Evolution API into MinIO with retries + size gate.
        attachment_id: str | None = None
        if media:
            mime_type, file_name = media
            attachment_row = conn.execute(
                text(
                    "INSERT INTO attachments (message_id, mime_type, file_name) "
                    "VALUES (:mid, :mime, :fname) RETURNING id"
                ),
                {"mid": inserted.id, "mime": mime_type, "fname": file_name},
            ).one()
            attachment_id = str(attachment_row.id)

        # RECEIVED -> VALIDATED: normalization succeeded (master §10.1)
        assert_valid_transition("message", MessageState.RECEIVED, MessageState.VALIDATED)
        conn.execute(
            text(
                "UPDATE messages SET processing_state = CAST('VALIDATED' AS message_state), "
                "updated_at = now() WHERE id = :id"
            ),
            {"id": inserted.id},
        )
        conn.execute(
            text(
                "INSERT INTO raw_event_payloads "
                "(message_id, source, payload, retention_expires_at) "
                "VALUES (:mid, 'evolution', CAST(:payload AS jsonb), :retention)"
            ),
            {
                "mid": inserted.id,
                "payload": _json(raw),
                "retention": dt.datetime.now(dt.UTC)
                + dt.timedelta(days=settings.raw_message_retention_days),
            },
        )
    return "stored", str(inserted.id), attachment_id


def _json(value: dict) -> str:
    import json

    return json.dumps(value, default=str)


# --- P9 dedup cascade (FR-DED-001..003, DEC-009) --------------------------------


def _image_bytes(item: dict) -> bytes | None:
    """Inline media bytes from the webhook payload (Evolution base64=true);
    None on any decode problem — dedup must not break ingestion."""
    candidates = [item, item.get("message") or {}]
    for source in candidates:
        if not isinstance(source, dict):
            continue
        value = source.get("base64")
        if isinstance(value, str) and value:
            try:
                return base64.b64decode(value.split(",", 1)[-1])
            except Exception:  # noqa: BLE001
                return None
    message = item.get("message") or {}
    for key in MEDIA_KEYS:
        media = message.get(key)
        if key == "documentWithCaptionMessage":
            media = (message.get("documentWithCaptionMessage") or {}).get(
                "message", {}
            ).get("documentMessage")
        if isinstance(media, dict) and media.get("base64"):
            try:
                return base64.b64decode(str(media["base64"]).split(",", 1)[-1])
            except Exception:  # noqa: BLE001
                return None
    return None


def _company_keys_in_text(text_value: str | None, known_keys: list[str]) -> set[str]:
    """Known companies whose normalized_key tokens appear in the text — the
    FR-DED-002 company-agreement gate (see pia_shared.dedup docstring)."""
    tokens = set(re.findall(r"[a-z0-9]+", (text_value or "").lower()))
    return {key for key in known_keys if key and set(key.split()) <= tokens}


def _dedup_verdict(
    conn, message_id: str, group_id: str, text_value: str | None,
    chash: str, phash_hex: str | None,
) -> tuple[DedupVerdict, str | None]:
    """Exact (FR-DED-001, 7-day reminder rule) -> visual (DEC-009) -> near
    (FR-DED-002, company-gated). Returns (verdict, original_message_id)."""
    settings = get_settings()

    exact_row = conn.execute(
        text(
            "SELECT id, EXTRACT(EPOCH FROM (now() - sent_at)) / 86400.0 AS age_days "
            "FROM messages WHERE group_id = CAST(:g AS uuid) AND content_hash = :h "
            "AND id <> CAST(:id AS uuid) ORDER BY sent_at DESC LIMIT 1"
        ),
        {"g": group_id, "h": chash, "id": message_id},
    ).mappings().first()
    exact = decide_exact_duplicate(
        float(exact_row["age_days"]) if exact_row else None,
        settings.dedup_exact_reminder_days,
    )
    if exact.suppress:
        return exact, (str(exact_row["id"]) if exact_row else None)

    if phash_hex:
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(
            hours=settings.dedup_near_window_hours
        )
        candidates = conn.execute(
            text(
                "SELECT id, image_phash FROM messages "
                "WHERE group_id = CAST(:g AS uuid) AND image_phash IS NOT NULL "
                "AND id <> CAST(:id AS uuid) AND sent_at >= :cutoff"
            ),
            {"g": group_id, "id": message_id, "cutoff": cutoff},
        ).all()
        distances = [
            (hamming_hex(phash_hex, row.image_phash) or 64, str(row.id))
            for row in candidates
        ]
        if distances:
            best_distance, best_id = min(distances, key=lambda pair: pair[0])
            visual = decide_visual_duplicate(best_distance)
            if visual.suppress:
                return visual, best_id

    near_cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(
        hours=settings.dedup_near_window_hours
    )
    recent = conn.execute(
        text(
            "SELECT id, text FROM messages WHERE group_id = CAST(:g AS uuid) "
            "AND id <> CAST(:id AS uuid) AND text IS NOT NULL AND length(text) > 40 "
            "AND sent_at >= :cutoff AND (classification IS NULL OR "
            "classification->>'status' IS DISTINCT FROM 'duplicate') "
            "ORDER BY sent_at DESC LIMIT 100"
        ),
        {"g": group_id, "id": message_id, "cutoff": near_cutoff},
    ).all()
    known_keys = [
        row.normalized_key for row in conn.execute(
            text("SELECT normalized_key FROM companies")
        )
    ]
    new_keys = _company_keys_in_text(text_value, known_keys)
    for row in recent:
        similarity = token_jaccard(text_value, row.text)
        verdict = decide_near_duplicate(
            similarity, new_keys, _company_keys_in_text(row.text, known_keys),
            settings.dedup_near_threshold,
        )
        if verdict.suppress:
            return verdict, str(row.id)
    return DedupVerdict(False, None, ""), None


def _mark_suppressed(conn, message_id: str, verdict, of_message_id: str | None) -> None:
    """Persist the suppression visibly: walk §10.1 to PROCESSED with the dedup
    annotation, audit the decision, bump the metric. No jobs are enqueued."""
    current = MessageState.RECEIVED
    for target in (MessageState.VALIDATED, MessageState.QUEUED,
                   MessageState.PROCESSING, MessageState.PROCESSED):
        assert_valid_transition("message", current, target)
        current = target
    conn.execute(
        text(
            "UPDATE messages SET processing_state = "
            "CAST('PROCESSED' AS message_state), classification = "
            "CAST(:cls AS jsonb), updated_at = now() WHERE id = CAST(:id AS uuid)"
        ),
        {
            "id": message_id,
            "cls": _json({"status": "duplicate", "layer": verdict.layer,
                          "reason": verdict.reason, "of_message_id": of_message_id,
                          "rule": "FR-DED-001..003 + DEC-009"}),
        },
    )
    conn.execute(
        text(
            "INSERT INTO audit_logs (actor, action, entity_type, entity_id, result, "
            "metadata) VALUES ('api:webhook', 'dedup.suppressed', 'messages', "
            "CAST(:id AS uuid), 'ok', CAST(:meta AS jsonb))"
        ),
        {"id": message_id,
         "meta": _json({"layer": verdict.layer, "reason": verdict.reason,
                        "of_message_id": of_message_id})},
    )
    try:
        client = redis_lib.Redis.from_url(
            get_settings().redis_url, socket_connect_timeout=1
        )
        count = increment(client, "duplicate_suppressed_total",
                          layer=verdict.layer or "unknown")
        logger.info("duplicate_suppressed", message_id=message_id,
                    layer=verdict.layer, reason=verdict.reason, counter=count)
    except Exception as exc:  # noqa: BLE001 — metrics are best-effort
        logger.warning("dedup_metric_failed", error=str(exc)[:120])
