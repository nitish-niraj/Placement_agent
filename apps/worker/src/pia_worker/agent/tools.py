"""Stage 1 read-only tool registry (ADR-011). Each tool wraps deterministic
SQL that already exists in the pipeline/dashboard; results are plain dicts,
citable where they carry message ids, and size-capped so one tool can never
blow the scratchpad. Tools NEVER mutate state (ADR-003/SEC-005)."""

import sqlalchemy

from pia_shared.textnorm import normalize_name
from pia_worker.search.retrieval import keyword_matches

_MAX_ROWS = 8
_TEXT_CAP = 300


def _rows(result, cap: int = _MAX_ROWS) -> list[dict]:
    return [dict(r) for r in result.mappings().all()][:cap]


def _shorten(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, str) and len(v) > _TEXT_CAP:
            out[k] = v[:_TEXT_CAP] + "…"
        else:
            out[k] = v
    return out


def search_messages(conn: sqlalchemy.Connection, query: str) -> list[dict]:
    """Keyword search over stored messages (citable: rows carry message ids)."""
    rows = keyword_matches(conn, query, limit=6)
    for r in rows:
        r["text"] = (r.get("text") or "")[:_TEXT_CAP]
    return rows


def get_eligibility(conn: sqlalchemy.Connection, company: str | None = None) -> list[dict]:
    """Eligibility records, optionally filtered by company name fragment."""
    sql = ("SELECT c.canonical_name AS company, r.state, r.match_method, "
           "r.confidence, r.detected_at FROM eligibility_records r "
           "JOIN companies c ON c.id = r.company_id ")
    params: dict[str, object] = {}
    if company:
        sql += "WHERE c.canonical_name ILIKE :frag "
        first = normalize_name(company).split()
        params["frag"] = f"%{first[0] if first else company}%"
    sql += "ORDER BY r.detected_at DESC"
    return [_shorten(r) for r in _rows(conn.execute(sqlalchemy.text(sql), params), 20)]


def get_events(conn: sqlalchemy.Connection, company: str | None = None) -> list[dict]:
    """Recent events (type/status/deadline/drive details), optionally filtered."""
    sql = ("SELECT e.id, e.type, e.status, e.title, e.deadline_at, e.start_at, "
           "e.current_payload->>'designation' AS designation, "
           "e.current_payload->>'salary_package' AS salary_package, "
           "e.current_payload->>'job_location' AS job_location, "
           "e.current_payload->>'source_message_id' AS source_message_id, "
           "e.created_at, c.canonical_name AS company FROM events e "
           "LEFT JOIN companies c ON c.id = e.company_id ")
    params: dict[str, object] = {}
    if company:
        sql += "WHERE c.canonical_name ILIKE :frag "
        first = normalize_name(company).split()
        params["frag"] = f"%{first[0] if first else company}%"
    sql += "ORDER BY e.created_at DESC"
    return [_shorten(r) for r in _rows(conn.execute(sqlalchemy.text(sql), params))]


def get_deadlines(conn: sqlalchemy.Connection, state: str | None = None) -> list[dict]:
    """Deadlines with their event, optionally by state (OPEN/DUE_SOON/EXPIRED)."""
    sql = ("SELECT d.state, d.due_at, e.type, e.title, "
           "c.canonical_name AS company FROM deadlines d "
           "JOIN events e ON e.id = d.event_id "
           "LEFT JOIN companies c ON c.id = e.company_id ")
    params: dict[str, object] = {}
    if state:
        sql += "WHERE d.state::text = :state "
        params["state"] = state.upper()
    sql += "ORDER BY d.due_at"
    return [_shorten(r) for r in _rows(conn.execute(sqlalchemy.text(sql), params))]


def get_company_timeline(conn: sqlalchemy.Connection, company: str) -> list[dict]:
    """Events + update counts for one company (F-027, condensed)."""
    sql = ("SELECT e.type, e.status, e.title, e.deadline_at, e.created_at, "
           "(SELECT count(*) FROM event_updates u WHERE u.event_id = e.id) "
           "AS update_count, e.current_payload->>'source_message_id' "
           "AS source_message_id FROM events e "
           "JOIN companies c ON c.id = e.company_id "
           "WHERE c.canonical_name ILIKE :frag ORDER BY e.created_at DESC")
    frag = f"%{normalize_name(company).split()[0] if normalize_name(company) else company}%"
    return [_shorten(r) for r in _rows(conn.execute(sqlalchemy.text(sql), {"frag": frag}))]


def get_profile(conn: sqlalchemy.Connection) -> dict:
    """The student's profile — identifiers stay out (SEC-002: they are not
    needed for 'what should I do' answers and never leave the system)."""
    row = conn.execute(
        sqlalchemy.text(
            "SELECT p.canonical_name, p.branch, p.batch, p.cgpa, "
            "p.tenth_percent, p.twelfth_percent, p.backlog_count, u.email "
            "FROM candidate_profiles p JOIN users u ON u.id = p.user_id LIMIT 1"
        )
    ).mappings().first()
    return dict(row) if row else {}


def get_document(conn: sqlalchemy.Connection, document_id: str) -> dict:
    """One stored document's metadata by id."""
    row = conn.execute(
        sqlalchemy.text(
            "SELECT id, kind, storage_key, access_level, created_at "
            "FROM profile_documents WHERE id = CAST(:id AS uuid)"
        ),
        {"id": document_id},
    ).mappings().first()
    return dict(row) if row else {}


def get_applications(conn: sqlalchemy.Connection,
                     company: str | None = None) -> list[dict]:
    """Per company+role application answers: whether the student actually
    applied (APPLIED), said no (NOT_APPLIED), is unsure (NOT_SURE /
    ELIGIBLE_NOT_APPLIED / UNKNOWN), or is not interested. Company mentioned
    != applied — consult this before proposing any follow-up."""
    sql = ("SELECT c.canonical_name AS company, s.role_normalized AS role, "
           "s.status, s.applied_at, s.updated_at FROM application_states s "
           "JOIN companies c ON c.id = s.company_id ")
    params: dict[str, object] = {}
    if company:
        sql += "WHERE c.canonical_name ILIKE :frag "
        first = normalize_name(company).split()
        params["frag"] = f"%{first[0] if first else company}%"
    sql += "ORDER BY s.updated_at DESC"
    return [_shorten(r) for r in _rows(conn.execute(sqlalchemy.text(sql), params), 20)]


# The registry the agent prompt is built from (name -> one-line description).
TOOL_SPECS: dict[str, str] = {
    "search_messages": "search stored WhatsApp messages by keywords — the "
                       "citable source of truth (rows carry message ids)",
    "get_eligibility": "list eligibility records (optionally for one company)",
    "get_events": "list recent events (optionally for one company)",
    "get_deadlines": "list deadlines and their states (optionally filtered by "
                     "state: OPEN / DUE_SOON / EXPIRED)",
    "get_company_timeline": "one company's event history with update counts",
    "get_applications": "per company+role application answers (APPLIED / "
                        "NOT_APPLIED / NOT_SURE / NOT_INTERESTED / UNKNOWN) — "
                        "check before any follow-up proposal",
    "get_profile": "the student's profile (name, course, CGPA, email)",
    "get_document": "one stored document's metadata by id",
}


def run_tool(conn: sqlalchemy.Connection, name: str, args: dict[str, str]) -> list[dict] | dict:
    """Dispatch one tool call. Unknown tools return an observation (the loop
    continues — the model can correct itself); every result is size-capped."""
    if name == "search_messages":
        return search_messages(conn, str(args.get("query") or ""))
    if name == "get_eligibility":
        return get_eligibility(conn, args.get("company"))
    if name == "get_events":
        return get_events(conn, args.get("company"))
    if name == "get_deadlines":
        return get_deadlines(conn, args.get("state"))
    if name == "get_company_timeline":
        return get_company_timeline(conn, str(args.get("company") or ""))
    if name == "get_applications":
        return get_applications(conn, args.get("company"))
    if name == "get_profile":
        return get_profile(conn)
    if name == "get_document":
        return get_document(conn, str(args.get("document_id") or ""))
    return {"error": f"unknown tool: {name}"}
