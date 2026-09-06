"""FR-MEM-003 mention association + FR-MEM-005 watch priority bump.

Called from process_message for allowlisted messages whose classification
produced company entities. Deterministic: each mention resolves through the
F-017 ladder and is persisted as a memory_records row (the company timeline
store), while watched companies trigger a deterministic priority bump —
rules over LLM output (FR-CLS-004 posture).
"""

import json

import sqlalchemy
import structlog

from pia_shared.enums import Importance
from pia_worker.companies.resolver import CompanyRef, resolve_company

logger = structlog.get_logger()

MENTION_CONFIDENCE = 0.9  # deterministic resolution, not extraction confidence


def watch_bump(importance: Importance) -> Importance | None:
    """FR-MEM-005: watched-company messages route to HIGH priority without any
    manual subscription. CRITICAL/HIGH are never touched (no downgrade); only
    low-priority classifications are raised. Returns the bumped importance or
    None when no bump applies."""
    if importance in (Importance.MEDIUM, Importance.LOW, Importance.IGNORE):
        return Importance.HIGH
    return None


def associate_message_companies(
    conn: sqlalchemy.Connection, message_id: str, mention_names: list[str]
) -> list[CompanyRef]:
    """Resolve every company mention for one message and persist the linkage
    as memory_records (scope COMPANY, subject 'company:<key>', predicate
    'mention'). Idempotent per (message, company). Never creates duplicates
    for repeated mentions of the same company."""
    refs: list[CompanyRef] = []
    seen: set[str] = set()
    for raw in mention_names:
        if not raw or not raw.strip():
            continue
        ref = resolve_company(conn, raw)
        if ref is None or ref.company_id in seen:
            continue
        seen.add(ref.company_id)
        _record_mention(conn, message_id, ref, raw)
        refs.append(ref)
    return refs


def _record_mention(
    conn: sqlalchemy.Connection, message_id: str, ref: CompanyRef, raw_name: str
) -> None:
    subject = f"company:{ref.normalized_key}"
    exists = conn.execute(
        sqlalchemy.text(
            "SELECT 1 FROM memory_records WHERE scope = CAST('COMPANY' AS memory_scope) "
            "AND subject = :subject AND predicate = 'mention' "
            "AND source_message_id = CAST(:mid AS uuid) LIMIT 1"
        ),
        {"subject": subject, "mid": message_id},
    ).first()
    if exists is not None:
        return
    conn.execute(
        sqlalchemy.text(
            "INSERT INTO memory_records (scope, subject, predicate, object_value, "
            "source_message_id, confidence) "
            "VALUES (CAST('COMPANY' AS memory_scope), :subject, 'mention', "
            "CAST(:payload AS jsonb), CAST(:mid AS uuid), :confidence)"
        ),
        {
            "subject": subject,
            "payload": json.dumps({
                "mention": raw_name,
                "resolved_via": ref.via,
                "canonical": ref.canonical_name,
                "watch_state": ref.watch_state.value,
            }, default=str),
            "mid": message_id,
            "confidence": MENTION_CONFIDENCE,
        },
    )
    logger.info("company_mention_linked", message_id=message_id,
                company=ref.canonical_name, via=ref.via)
