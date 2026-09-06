"""F-016 eligibility record persistence (§10.2 state machine, FR-ELG-008/009).

DB-facing half of the Eligibility Engine: loads the identity profile (with
SEC-002 decryption), resolves/creates the minimal company record, and writes
ONE eligibility_records row per candidate list with every state change asserted
through pia_shared.states (no AMBIGUOUS -> ELIGIBLE edge exists — ADR-005).
"""

import json
from typing import Any

import sqlalchemy
import structlog

from pia_shared.crypto import decrypt
from pia_shared.enums import EligibilityState
from pia_shared.states import assert_valid_transition
from pia_shared.textnorm import normalize_name
from pia_worker.companies.resolver import (
    activate_watch,
)
from pia_worker.companies.resolver import (
    resolve_company as resolve_company_ref,
)
from pia_worker.eligibility.matcher import IdentityProfile, ListOutcome
from pia_worker.settings import get_settings

logger = structlog.get_logger()


def load_identity_profile(conn: sqlalchemy.Connection) -> tuple[IdentityProfile, str] | None:
    """Profile + user_id for matching; returns None when no profile exists."""
    row = conn.execute(
        sqlalchemy.text(
            "SELECT p.id, p.user_id, p.canonical_name, p.roll_number, "
            "p.registration_number, p.student_id, p.branch, p.batch "
            "FROM candidate_profiles p LIMIT 1"
        )
    ).mappings().first()
    if row is None:
        return None
    secret = get_settings().pia_encryption_key
    aliases = [
        r.alias for r in conn.execute(
            sqlalchemy.text("SELECT alias FROM identity_aliases WHERE profile_id = :pid"),
            {"pid": row["id"]},
        )
    ]
    # SEC-002 read path: sensitive identifiers decrypt transparently (plaintext
    # legacy values pass through unchanged).
    profile = IdentityProfile(
        profile_id=str(row["id"]),
        canonical_name=row["canonical_name"],
        normalized_names=frozenset(
            [normalize_name(row["canonical_name"])] + [normalize_name(a) for a in aliases]
        ),
        registration_number=decrypt(row["registration_number"], secret),
        roll_number=decrypt(row["roll_number"], secret),
        student_id=decrypt(row["student_id"], secret),
        branch=row["branch"],
        batch=row["batch"],
        aliases=tuple(aliases),
    )
    return profile, str(row["user_id"])


def resolve_company(conn: sqlalchemy.Connection, company_name: str) -> str:
    """Delegates to the F-017 resolver (aliases + containment fallback). The
    P6-era inline creation lived here; the company module owns it now."""
    ref = resolve_company_ref(conn, company_name)
    if ref is None:
        raise ValueError(f"cannot resolve blank company name: {company_name!r}")
    return ref.company_id


def find_existing_record(conn: sqlalchemy.Connection, extraction_id: str) -> str | None:
    """Idempotency anchor: one match run per extraction (re-runs are no-ops)."""
    row = conn.execute(
        sqlalchemy.text(
            "SELECT id FROM eligibility_records WHERE source_document_id = CAST(:eid AS uuid)"
        ),
        {"eid": extraction_id},
    ).first()
    return str(row.id) if row is not None else None


def _evidence_payload(outcome: ListOutcome, thresholds: Any) -> dict[str, Any]:
    """Auditable evidence bundle (SEC-009): observed row refs + the deterministic
    rationale + the evaluation context. Never contains LLM output."""
    return {
        "rationale": outcome.rationale,
        "refs": [e.model_dump(mode="json") for e in outcome.evidence],
        "rows_evaluated": len(outcome.row_results),
        "rows_cited": len(outcome.evidence),
        "thresholds": {"fuzzy": thresholds.fuzzy, "ambiguous": thresholds.ambiguous},
    }


def persist_outcome(
    conn: sqlalchemy.Connection,
    *,
    user_id: str,
    extraction_id: str,
    message_id: str | None,
    company_id: str,
    outcome: ListOutcome,
    thresholds: Any,
) -> str:
    """Insert the eligibility record walking §10.2 from UNKNOWN; every step is
    asserted (InvalidTransitionError would abort the transaction) and audited."""
    assert_valid_transition("eligibility", EligibilityState.UNKNOWN, outcome.state)
    state_chain = [EligibilityState.UNKNOWN, outcome.state]
    if outcome.state is EligibilityState.MATCHED:
        # MATCHED -> ELIGIBLE for identifier/name-decisive evidence (§10.2).
        assert_valid_transition("eligibility", EligibilityState.MATCHED, EligibilityState.ELIGIBLE)
        state_chain.append(EligibilityState.ELIGIBLE)
    final_state = state_chain[-1]

    if final_state is EligibilityState.ELIGIBLE:
        # F-018 / FR-MEM-005: eligibility activates the company watch and
        # advances the lifecycle — same transaction, so the record and the
        # watch state are always consistent.
        activate_watch(conn, company_id)

    record_id = str(conn.execute(
        sqlalchemy.text(
            "INSERT INTO eligibility_records (user_id, company_id, state, match_status, "
            "match_method, confidence, requires_user_review, source_message_id, "
            "source_document_id, evidence) "
            "VALUES (CAST(:user AS uuid), CAST(:company AS uuid), "
            "CAST(:state AS eligibility_state), "
            "CAST(:status AS match_status), CAST(:method AS match_method), :confidence, "
            ":review, CAST(:message AS uuid), CAST(:extraction AS uuid), "
            "CAST(:evidence AS jsonb)) "
            "RETURNING id"
        ),
        {
            "user": user_id,
            "company": company_id,
            "state": final_state.value,
            "status": outcome.status.value,
            "method": outcome.match_method.value if outcome.match_method else None,
            "confidence": outcome.confidence,
            "review": outcome.requires_user_review,
            "message": message_id,
            "extraction": extraction_id,
            "evidence": json.dumps(_evidence_payload(outcome, thresholds), default=str),
        },
    ).scalar_one())

    conn.execute(
        sqlalchemy.text(
            "INSERT INTO audit_logs (actor, action, entity_type, entity_id, result, metadata) "
            "VALUES ('worker:eligibility', 'eligibility.state_change', 'eligibility_records', "
            "CAST(:id AS uuid), 'ok', CAST(:meta AS jsonb))"
        ),
        {
            "id": record_id,
            "meta": json.dumps({
                "chain": [s.value for s in state_chain],
                "match_status": outcome.status.value,
                "match_method": outcome.match_method.value if outcome.match_method else None,
                "confidence": outcome.confidence,
                "rationale": outcome.rationale,
            }, default=str),
        },
    )
    return record_id