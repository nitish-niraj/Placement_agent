"""P12 job: embed enable-group messages into pgvector (F-028, ADR-003).

Runs hourly alongside the deadline sweep. NIM flakiness is absorbed: a failed
batch simply leaves those messages for the next run (never fails the sweep).
Embeddings are retrieval context ONLY — never authoritative for eligibility or
deadlines (ADR-003).
"""

import sqlalchemy
import structlog

from pia_worker.ai.provider import NIMProvider, ProviderError
from pia_worker.jobs.process_message import _engine
from pia_worker.settings import get_settings

logger = structlog.get_logger()

_BATCH_LIMIT = 48
_MIN_TEXT_LEN = 20
_MAX_TEXT_CHARS = 1500  # nv-embedqa-e5-v5 context is ~512 tokens


def embed_messages(batch_size: int = _BATCH_LIMIT) -> dict:
    settings = get_settings()
    provider = NIMProvider()
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(
            sqlalchemy.text(
                "SELECT m.id, m.text FROM messages m "
                "JOIN groups g ON g.id = m.group_id "
                "WHERE m.text IS NOT NULL AND g.enabled AND length(m.text) >= :minlen "
                "AND NOT EXISTS (SELECT 1 FROM message_embeddings me "
                "WHERE me.message_id = m.id) "
                "ORDER BY m.sent_at DESC LIMIT :lim"
            ),
            {"minlen": _MIN_TEXT_LEN, "lim": batch_size},
        ).mappings().all()
    if not rows:
        return {"embedded": 0, "remaining": 0}

    try:
        vectors = provider.embed([r["text"][:_MAX_TEXT_CHARS] for r in rows])
    except ProviderError as exc:
        logger.warning("embed_batch_failed", error=str(exc)[:120])
        return {"embedded": 0, "error": str(exc)[:120]}

    with engine.begin() as conn:
        for row, vector in zip(rows, vectors, strict=True):
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO message_embeddings (message_id, embedding, model) "
                    "VALUES (CAST(:id AS uuid), CAST(:v AS vector), :model) "
                    "ON CONFLICT (message_id) DO NOTHING"
                ),
                {"id": str(row["id"]),
                 "v": "[" + ",".join(f"{x:.6f}" for x in vector) + "]",
                 "model": settings.embedding_model},
            )
    remaining = conn.execute(
        sqlalchemy.text(
            "SELECT count(*) FROM messages m JOIN groups g ON g.id = m.group_id "
            "WHERE m.text IS NOT NULL AND g.enabled AND length(m.text) >= :minlen "
            "AND NOT EXISTS (SELECT 1 FROM message_embeddings me "
            "WHERE me.message_id = m.id)"
        ),
        {"minlen": _MIN_TEXT_LEN},
    ).scalar()
    logger.info("messages_embedded", count=len(rows), remaining=remaining)
    return {"embedded": len(rows), "remaining": int(remaining or 0)}
