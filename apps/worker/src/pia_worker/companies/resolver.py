"""F-017 company canonicalization + FR-MEM-003 mention resolution.

Deterministic only — no LLM. Resolution ladder for a printed/mentioned name:

1. exact ``normalized_key`` match          -> existing company
2. exact alias match (``company_aliases``) -> existing company
3. token-containment fallback              -> existing company ("Techademy"
   ⊂ "Techademy Learning Solutions Pvt. Ltd.", "Accenture India" ⊃ "Accenture")
4. no match                                -> create company + register the name

Every resolved name self-heals into ``company_aliases`` (FR-MEM-001: variants
map to one entity), so step 3 only has to fire once per variant.
"""

import json
from dataclasses import dataclass

import sqlalchemy
import structlog

from pia_shared.enums import CompanyLifecycle, CompanyWatch
from pia_shared.states import assert_valid_transition
from pia_shared.textnorm import normalize_name

logger = structlog.get_logger()


@dataclass(frozen=True)
class CompanyRef:
    company_id: str
    canonical_name: str
    normalized_key: str
    watch_state: CompanyWatch
    via: str  # 'key' | 'alias' | 'containment' | 'created'


# Legal-form suffixes carry no identity ("Pvt. Ltd." ≡ "Private Limited") —
# stripped for containment comparison only; the stored key keeps every token.
_CORPORATE_SUFFIXES = {"pvt", "private", "ltd", "limited", "llp", "inc", "corp", "co"}


def _comparison_tokens(key: str) -> frozenset[str]:
    tokens = frozenset(key.split())
    stripped = tokens - _CORPORATE_SUFFIXES
    return stripped or tokens


def find_containment_candidate(
    key: str, known_keys: list[tuple[str, str]]
) -> str | None:
    """Pure stage-3 helper: the id of the company whose key tokens contain (or
    are contained in) the mention's tokens, else None. Requires non-empty token
    overlap; single-token keys only match when equal (handled upstream)."""
    mention_tokens = _comparison_tokens(key)
    if not mention_tokens:
        return None
    for company_id, known in known_keys:
        known_tokens = _comparison_tokens(known)
        if not known_tokens:
            continue
        if known_tokens <= mention_tokens or mention_tokens <= known_tokens:
            if known == key:  # exact hit — never reaches this helper
                continue
            return company_id
    return None


def register_alias(conn: sqlalchemy.Connection, company_id: str, name: str) -> None:
    alias = normalize_name(name)
    if not alias:
        return
    conn.execute(
        sqlalchemy.text(
            "INSERT INTO company_aliases (company_id, alias) "
            "VALUES (CAST(:cid AS uuid), :alias) ON CONFLICT DO NOTHING"
        ),
        {"cid": company_id, "alias": alias},
    )


def _ref_from_row(row, via: str) -> CompanyRef:
    return CompanyRef(
        company_id=str(row.id),
        canonical_name=row.canonical_name,
        normalized_key=row.normalized_key,
        watch_state=CompanyWatch(row.watch_state),
        via=via,
    )


def resolve_company(
    conn: sqlalchemy.Connection, name: str, create: bool = True
) -> CompanyRef | None:
    """Resolve a company name (FR-MEM-003) or create it (FR-MEM-001).
    Returns None only for blank input or create=False with no match."""
    key = normalize_name(name)
    if not key:
        return None

    row = conn.execute(
        sqlalchemy.text(
            "SELECT id, canonical_name, normalized_key, watch_state FROM companies "
            "WHERE normalized_key = :key"
        ),
        {"key": key},
    ).first()
    if row is not None:
        ref = _ref_from_row(row, via="key")
        register_alias(conn, ref.company_id, name)  # self-heal missing aliases
        return ref

    row = conn.execute(
        sqlalchemy.text(
            "SELECT c.id, c.canonical_name, c.normalized_key, c.watch_state "
            "FROM company_aliases a JOIN companies c ON c.id = a.company_id "
            "WHERE a.alias = :key"
        ),
        {"key": key},
    ).first()
    if row is not None:
        return _ref_from_row(row, via="alias")

    known = conn.execute(
        sqlalchemy.text("SELECT id, normalized_key FROM companies")
    ).all()
    hit = find_containment_candidate(key, [(str(r.id), r.normalized_key) for r in known])
    if hit is not None:
        row = conn.execute(
            sqlalchemy.text(
                "SELECT id, canonical_name, normalized_key, watch_state FROM companies "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": hit},
        ).first()
        ref = _ref_from_row(row, via="containment")
        register_alias(conn, ref.company_id, name)
        logger.info("company_resolved_by_containment", mention=name,
                    canonical=ref.canonical_name)
        return ref

    if not create:
        return None
    row = conn.execute(
        sqlalchemy.text(
            "INSERT INTO companies (canonical_name, normalized_key, first_seen_at, "
            "lifecycle_stage) VALUES (:name, :key, now(), :stage) "
            "RETURNING id, canonical_name, normalized_key, watch_state"
        ),
        {"name": name.strip(), "key": key, "stage": CompanyLifecycle.DISCOVERED.value},
    ).one()
    register_alias(conn, str(row.id), name)
    ref = _ref_from_row(row, via="created")
    logger.info("company_created", company=ref.canonical_name, key=ref.normalized_key)
    return ref


def activate_watch(conn: sqlalchemy.Connection, company_id: str) -> dict[str, str | None]:
    """F-018 / FR-MEM-005: an ELIGIBLE company is watched automatically, no
    manual re-subscription. Also advances the application lifecycle to ELIGIBLE
    (FR-MEM-004, DISCOVERED -> ELIGIBLE; asserted, reversible via correction).
    Idempotent: re-running on an already-watching company writes nothing."""
    row = conn.execute(
        sqlalchemy.text(
            "SELECT watch_state, lifecycle_stage FROM companies "
            "WHERE id = CAST(:id AS uuid)"
        ),
        {"id": company_id},
    ).first()
    if row is None:
        raise ValueError(f"company {company_id} does not exist")
    changes: dict[str, str | None] = {}
    if CompanyWatch(row.watch_state) is CompanyWatch.NONE:
        conn.execute(
            sqlalchemy.text(
                "UPDATE companies SET watch_state = "
                "CAST('WATCHING' AS company_watch), updated_at = now() "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": company_id},
        )
        changes["watch_state"] = CompanyWatch.WATCHING.value
    current_stage = CompanyLifecycle(row.lifecycle_stage) if row.lifecycle_stage \
        else CompanyLifecycle.DISCOVERED
    if current_stage is not CompanyLifecycle.ELIGIBLE:
        assert_valid_transition("company_lifecycle", current_stage,
                                CompanyLifecycle.ELIGIBLE)
        conn.execute(
            sqlalchemy.text(
                "UPDATE companies SET lifecycle_stage = :stage, updated_at = now() "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": company_id, "stage": CompanyLifecycle.ELIGIBLE.value},
        )
        changes["lifecycle_stage"] = CompanyLifecycle.ELIGIBLE.value
    if changes:
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO audit_logs (actor, action, entity_type, entity_id, "
                "result, metadata) VALUES ('worker:eligibility', "
                "'company.watch_activated', 'companies', CAST(:id AS uuid), 'ok', "
                "CAST(:meta AS jsonb))"
            ),
            {"id": company_id, "meta": json.dumps({"changes": changes}, default=str)},
        )
    return changes
