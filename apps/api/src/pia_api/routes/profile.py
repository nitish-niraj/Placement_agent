"""Candidate profile API + match-correction feedback (FR-PRO-001..004).

- GET/POST /api/v1/profile — read (decrypted) / upsert with audit rows (FR-PRO-001).
- POST /api/v1/feedback/match — supervised correction of match results (FR-PRO-004),
  state transitions validated by the §10.2 machine; every correction audited.
- SEC-002: roll/registration/student-id are encrypted at rest when
  PIA_ENCRYPTION_KEY is configured; plaintext legacy values decrypt transparently.
"""

import json
import uuid

import sqlalchemy
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from pia_api.db import get_engine
from pia_api.deps import require_dashboard_token
from pia_shared.crypto import decrypt, encrypt, is_encrypted
from pia_shared.enums import EligibilityState
from pia_shared.states import InvalidTransitionError, assert_valid_transition

router = APIRouter(
    prefix="/api/v1", tags=["profile"], dependencies=[Depends(require_dashboard_token)]
)

SENSITIVE_FIELDS = ("roll_number", "registration_number", "student_id")

PROFILE_UPSERT_SQL = sqlalchemy.text(
    "UPDATE candidate_profiles SET "
    "canonical_name = COALESCE(:canonical_name, canonical_name), "
    "roll_number = COALESCE(:roll_number, roll_number), "
    "registration_number = COALESCE(:registration_number, registration_number), "
    "student_id = COALESCE(:student_id, student_id), "
    "branch = COALESCE(:branch, branch), "
    "batch = COALESCE(:batch, batch), "
    "cgpa = COALESCE(:cgpa, cgpa), "
    "tenth_percent = COALESCE(:tenth_percent, tenth_percent), "
    "twelfth_percent = COALESCE(:twelfth_percent, twelfth_percent), "
    "backlog_count = COALESCE(:backlog_count, backlog_count), "
    "updated_at = now() "
    "WHERE user_id = (SELECT id FROM users WHERE display_name = :display_name "
    "OR true LIMIT 1) RETURNING id, user_id"
)


class ProfileUpdate(BaseModel):
    canonical_name: str | None = None
    roll_number: str | None = None
    registration_number: str | None = None
    student_id: str | None = None
    branch: str | None = None
    batch: str | None = None
    cgpa: float | None = Field(default=None, ge=0, le=10)
    tenth_percent: float | None = Field(default=None, ge=0, le=100)
    twelfth_percent: float | None = Field(default=None, ge=0, le=100)
    backlog_count: int | None = Field(default=None, ge=0)
    aliases: list[str] | None = None


class MatchFeedback(BaseModel):
    eligibility_record_id: str
    correction: str  # confirm | deny | wrong_match | missed_match
    note: str | None = None


def _audit(
    conn: sqlalchemy.Connection, actor: str, action: str,
    entity_type: str, entity_id: str, metadata: dict,
) -> None:
    conn.execute(
        sqlalchemy.text(
            "INSERT INTO audit_logs (actor, action, entity_type, entity_id, result, metadata) "
            "VALUES (:actor, :action, :etype, CAST(:eid AS uuid), 'ok', CAST(:meta AS jsonb))"
        ),
        {"actor": actor, "action": action, "etype": entity_type, "eid": entity_id,
         "meta": json.dumps(metadata, default=str)},
    )


def _get_profile_secret() -> str:
    from pia_api.settings import get_settings

    return get_settings().pia_encryption_key


@router.get("/profile")
def get_profile() -> dict:
    secret = _get_profile_secret()
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            sqlalchemy.text(
                "SELECT p.id, p.user_id, u.display_name, u.email, p.canonical_name, "
                "p.roll_number, p.registration_number, p.student_id, p.branch, p.batch, "
                "p.cgpa, p.tenth_percent, p.twelfth_percent, p.backlog_count, "
                "p.other_attributes FROM candidate_profiles p "
                "JOIN users u ON u.id = p.user_id LIMIT 1"
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="profile not found")
        aliases = conn.execute(
            sqlalchemy.text(
                "SELECT alias, kind FROM identity_aliases "
                "WHERE profile_id = :pid ORDER BY alias"
            ),
            {"pid": row["id"]},
        ).mappings().all()
        documents = conn.execute(
            sqlalchemy.text(
                "SELECT id, kind, storage_key, access_level, created_at "
                "FROM profile_documents WHERE profile_id = :pid AND deleted_at IS NULL"
            ),
            {"pid": row["id"]},
        ).mappings().all()

    profile = dict(row)
    for field in SENSITIVE_FIELDS:  # decrypt for display (SEC-002 read path)
        profile[field] = decrypt(profile[field], secret)
    profile["encrypted_at_rest"] = {f: is_encrypted(row[f]) for f in SENSITIVE_FIELDS}
    return {
        "profile": profile,
        "aliases": [dict(a) for a in aliases],
        "documents": [dict(d) for d in documents],
    }


@router.post("/profile")
def update_profile(payload: ProfileUpdate) -> dict:
    secret = _get_profile_secret()
    changes = {k: v for k, v in payload.model_dump(exclude_none=True).items()
               if k != "aliases"}
    if not changes and payload.aliases is None:
        raise HTTPException(status_code=422, detail="no fields to update")

    engine = get_engine()
    with engine.begin() as conn:
        # All bind params must exist (None = keep current value via COALESCE)
        fields = ["canonical_name", "roll_number", "registration_number",
                  "student_id", "branch", "batch", "cgpa", "tenth_percent",
                  "twelfth_percent", "backlog_count"]
        params = {f: changes.get(f) for f in fields}
        for field in SENSITIVE_FIELDS:  # SEC-002: encrypt sensitive fields at rest
            if params.get(field) is not None:
                params[field] = encrypt(params[field], secret)
        row = conn.execute(PROFILE_UPSERT_SQL, {
            **params,
            "display_name": "Nitish Kumar",
        }).one()
        profile_id = str(row.id)

        alias_changes: list[str] = []
        if payload.aliases:
            for alias in payload.aliases:
                conn.execute(
                    sqlalchemy.text(
                        "INSERT INTO identity_aliases (profile_id, alias) "
                        "VALUES (CAST(:p AS uuid), :a) ON CONFLICT DO NOTHING"
                    ),
                    {"p": profile_id, "a": alias.lower()},
                )
                alias_changes.append(alias.lower())
        _audit(conn, "user", "profile.update", "candidate_profiles", profile_id,
               {"changed": list(changes), "aliases_added": alias_changes})

    return {"status": "updated", "profile_id": profile_id,
            "fields": list(changes), "aliases_added": alias_changes}


def _activate_watch(conn: sqlalchemy.Connection, company_id: str) -> None:
    """F-018 / FR-MEM-005: user-confirmed eligibility activates the company
    watch and advances the lifecycle (DISCOVERED -> ELIGIBLE, asserted).
    Mirrors the worker's activate_watch — the API never imports worker code."""
    from pia_shared.enums import CompanyLifecycle, CompanyWatch

    row = conn.execute(
        sqlalchemy.text(
            "SELECT watch_state, lifecycle_stage FROM companies "
            "WHERE id = CAST(:id AS uuid)"
        ),
        {"id": company_id},
    ).first()
    if row is None:
        return
    changes: dict[str, str] = {}
    if row.watch_state == CompanyWatch.NONE.value:
        conn.execute(
            sqlalchemy.text(
                "UPDATE companies SET watch_state = "
                "CAST('WATCHING' AS company_watch), updated_at = now() "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": company_id},
        )
        changes["watch_state"] = CompanyWatch.WATCHING.value
    try:
        current_stage = CompanyLifecycle(row.lifecycle_stage or "DISCOVERED")
    except ValueError:
        current_stage = None
    if current_stage is not None and current_stage is not CompanyLifecycle.ELIGIBLE:
        try:
            assert_valid_transition("company_lifecycle", current_stage,
                                    CompanyLifecycle.ELIGIBLE)
        except InvalidTransitionError:
            current_stage = None
        else:
            conn.execute(
                sqlalchemy.text(
                    "UPDATE companies SET lifecycle_stage = :stage, "
                    "updated_at = now() WHERE id = CAST(:id AS uuid)"
                ),
                {"id": company_id, "stage": CompanyLifecycle.ELIGIBLE.value},
            )
            changes["lifecycle_stage"] = CompanyLifecycle.ELIGIBLE.value
    if changes:
        _audit(conn, "user", "company.watch_activated", "companies", company_id,
               {"changes": changes})


@router.post("/feedback/match")
def feedback_match(payload: MatchFeedback) -> dict:
    """FR-PRO-004: supervised correction. Walks the §10.2 correction chain for
    the record's current state (pia_shared.states.correction_chain) — `confirm`
    on an AMBIGUOUS record is the ONLY path to ELIGIBLE for ambiguous matches
    (ADR-005/NFR-006); every step is state-machine asserted and audited."""
    from pia_shared.states import CORRECTION_CHAINS, correction_chain

    decision = payload.correction
    if decision not in CORRECTION_CHAINS:
        raise HTTPException(
            status_code=422,
            detail=f"correction must be one of {sorted(CORRECTION_CHAINS)}",
        )
    try:
        record_uuid = str(uuid.UUID(payload.eligibility_record_id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="eligibility_record_id is not a uuid") from exc

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            sqlalchemy.text(
                "SELECT id, state, company_id FROM eligibility_records "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": record_uuid},
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="eligibility record not found")
        actual = EligibilityState(row.state)
        chain = correction_chain(decision, actual)
        if not chain:
            applicable = sorted(
                state.value for state in CORRECTION_CHAINS[decision]
            )
            raise HTTPException(
                status_code=409,
                detail=f"'{decision}' applies to states {applicable}, "
                       f"record is {actual.value}",
            )
        current = actual
        try:
            for target in chain:
                assert_valid_transition("eligibility", current, target)
                current = target
        except InvalidTransitionError as exc:  # defense in depth — chains are pre-validated
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        conn.execute(
            sqlalchemy.text(
                "UPDATE eligibility_records SET state = CAST(:s AS eligibility_state), "
                "correction_note = :note, requires_user_review = false, updated_at = now() "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"s": current.value, "note": payload.note, "id": payload.eligibility_record_id},
        )
        if current is EligibilityState.ELIGIBLE:
            # F-018: user-confirmed eligibility activates the company watch.
            _activate_watch(conn, str(row.company_id))
        _audit(conn, "user", "match.correction", "eligibility_records",
               payload.eligibility_record_id,
               {"correction": decision, "from": actual.value, "to": current.value,
                "chain": [s.value for s in chain], "note": payload.note})

    return {"status": "corrected", "from": actual.value, "to": current.value,
            "chain": [s.value for s in chain]}
