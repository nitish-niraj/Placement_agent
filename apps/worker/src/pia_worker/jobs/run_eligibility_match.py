"""P6 job: run the identity ladder over a parsed candidate list (F-015/F-016).

Chained automatically after parse_document when detection flagged the document
as a candidate list. Writes ONE eligibility_records row per list (idempotent per
extraction). No notifications here — that is P10 (F-023..F-025).

SEC-006: parse_document already refuses non-allowlisted groups; this job
re-checks the flag defensively so AI-derived state can never appear for a
group the owner did not select.
"""

import sqlalchemy
import structlog

from pia_shared.enums import EligibilityState, MatchStatus
from pia_shared.schemas import CandidateRow
from pia_worker.documents.types import collapse_ws
from pia_worker.eligibility.matcher import (
    ListOutcome,
    MatchThresholds,
    evaluate_list,
)
from pia_worker.eligibility.records import (
    find_existing_record,
    load_identity_profile,
    persist_outcome,
    resolve_company,
)
from pia_worker.jobs.process_message import PermanentJobError, _engine
from pia_worker.settings import get_settings

logger = structlog.get_logger()


def run_eligibility_match(extraction_id: str) -> str:
    settings = get_settings()
    thresholds = MatchThresholds(
        fuzzy=settings.fuzzy_match_threshold,
        ambiguous=settings.ambiguous_match_threshold,
        confidence_floor=settings.vision_confidence_floor,
    )
    engine = _engine()

    with engine.connect() as conn:
        extraction = conn.execute(
            sqlalchemy.text(
                "SELECT d.structured_payload, d.needs_review, a.message_id, "
                "g.enabled AS group_enabled "
                "FROM document_extractions d "
                "JOIN attachments a ON a.id = d.attachment_id "
                "JOIN messages m ON m.id = a.message_id "
                "JOIN groups g ON g.id = m.group_id "
                "WHERE d.id = CAST(:id AS uuid)"
            ),
            {"id": extraction_id},
        ).mappings().first()
        if extraction is None:
            raise PermanentJobError(f"document_extraction {extraction_id} does not exist")
        if find_existing_record(conn, extraction_id) is not None:
            return "already"  # idempotent re-run (NFR-002 posture)
        if not extraction["group_enabled"]:  # SEC-006 double-check
            logger.warning("eligibility_skipped_not_allowlisted", extraction_id=extraction_id)
            return "skipped_not_allowlisted"
        loaded = load_identity_profile(conn)
    if loaded is None:
        raise PermanentJobError("no candidate profile exists — matching impossible (FR-PRO-001)")
    profile, user_id = loaded

    payload = extraction["structured_payload"] or {}
    detection = payload.get("detection") or {}
    if not detection.get("is_candidate_list"):
        return "not_candidate_list"
    rows = [CandidateRow.model_validate(r) for r in payload.get("rows") or []]

    if rows:
        outcome = evaluate_list(rows, profile, thresholds)
    else:
        # Detection said "candidate list" but zero parseable rows: the source
        # cannot support a match decision (master §8.2 INVALID_SOURCE). State
        # stays UNKNOWN — there is no §10.2 transition to assert.
        outcome = ListOutcome(
            status=MatchStatus.INVALID_SOURCE,
            state=EligibilityState.UNKNOWN,
            match_method=None,
            confidence=0.0,
            requires_user_review=extraction["needs_review"],
            evidence=[],
            rationale="candidate list detected but no candidate rows parsed",
            row_results=[],
        )

    company_name = collapse_ws(detection.get("company") or "")
    if not company_name:
        # eligibility_records.company_id is NOT NULL — without any company
        # signal there is nothing to attach the decision to; the extraction
        # keeps the evidence and the gap is visible in logs.
        logger.warning("eligibility_skipped_no_company", extraction_id=extraction_id)
        return "skipped_no_company"

    with engine.begin() as conn:
        if find_existing_record(conn, extraction_id) is not None:
            return "already"  # re-check inside the write transaction
        company_id = resolve_company(conn, company_name)
        record_id = persist_outcome(
            conn,
            user_id=user_id,
            extraction_id=extraction_id,
            message_id=str(extraction["message_id"]),
            company_id=company_id,
            outcome=outcome,
            thresholds=thresholds,
        )

    if outcome.state.value == "ELIGIBLE":
        try:
            from redis import Redis
            from rq import Queue

            from pia_worker.queue import DEFAULT_QUEUE

            Queue(DEFAULT_QUEUE, connection=Redis.from_url(settings.redis_url)).enqueue(
                "pia_worker.jobs.notify.notify_eligibility", record_id
            )
        except Exception as exc:  # noqa: BLE001 — chaining failure logged
            logger.warning("notify_eligibility_enqueue_failed", record_id=record_id,
                           error=str(exc)[:120])

    logger.info(
        "eligibility_decided",
        extraction_id=extraction_id,
        record_id=record_id,
        state=outcome.state.value,
        match_status=outcome.status.value,
        match_method=outcome.match_method.value if outcome.match_method else None,
        confidence=outcome.confidence,
        requires_user_review=outcome.requires_user_review,
        company=company_name,
    )
    return f"eligibility:{outcome.state.value}"
