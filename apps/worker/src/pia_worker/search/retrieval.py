"""P12 conversational search (F-028) — context retrieval.

Hybrid retrieval for a natural-language question:
1. VECTOR: embed the question, take top-k messages by cosine distance from
   message_embeddings (ADR-003: semantic retrieval only — never authoritative
   for eligibility/deadlines decisions; here it only FINDS context).
2. KEYWORD: deterministic ILIKE scan over recent messages (always runs, so the
   feature degrades gracefully when embeddings or NIM are unavailable).
3. STRUCTURED: the user's eligibility records, active events with drive
   details, and watched companies — precise facts no prose answer should miss.

Everything returned is citable evidence; the answer layer (answer.py) may only
restate it.
"""

import sqlalchemy

from pia_shared.textnorm import normalize_name
from pia_worker.ai.provider import NIMProvider, ProviderError

_KEYWORD_TERMS_MIN_LEN = 3


def keyword_matches(
    conn: sqlalchemy.Connection, question: str, limit: int = 8
) -> list[dict]:
    """Recent messages containing any question token of length >= 3."""
    tokens = [
        t for t in normalize_name(question).split() if len(t) >= _KEYWORD_TERMS_MIN_LEN
    ]
    if not tokens:
        return []
    conditions = " OR ".join(
        f"text ILIKE :tok{i}" for i in range(len(tokens))
    )
    params: dict[str, object] = {"lim": limit}
    for i, token in enumerate(tokens):
        params[f"tok{i}"] = f"%{token}%"
    rows = conn.execute(
        sqlalchemy.text(
            "SELECT m.id, m.text, m.sent_at, g.name AS group_name FROM messages m "
            "JOIN groups g ON g.id = m.group_id "
            f"WHERE m.text IS NOT NULL AND ({conditions}) "
            "ORDER BY m.sent_at DESC LIMIT :lim"
        ),
        params,
    ).mappings().all()
    return [dict(r) for r in rows]


def vector_matches(
    conn: sqlalchemy.Connection, question: str, provider: NIMProvider,
    limit: int = 6,
) -> tuple[list[dict], str | None]:
    """Top-k messages by embedding cosine distance. Returns (matches, error) —
    error is set when embeddings are unavailable (vector path skipped)."""
    try:
        [question_vector] = provider.embed([question[:1500]])
    except ProviderError as exc:
        return [], str(exc)
    vector_text = "[" + ",".join(f"{v:.6f}" for v in question_vector) + "]"
    rows = conn.execute(
        sqlalchemy.text(
            "SELECT m.id, m.text, m.sent_at, g.name AS group_name, "
            "me.embedding <=> CAST(:qv AS vector) AS distance "
            "FROM message_embeddings me JOIN messages m ON m.id = me.message_id "
            "JOIN groups g ON g.id = m.group_id "
            "ORDER BY me.embedding <=> CAST(:qv AS vector) LIMIT :lim"
        ),
        {"qv": vector_text, "lim": limit},
    ).mappings().all()
    return [dict(r) for r in rows], None


def structured_facts(conn: sqlalchemy.Connection) -> dict:
    """Precise stored facts: eligibility, active events with drive details,
    watched companies. These anchor the answer in authoritative state."""
    eligibility = conn.execute(
        sqlalchemy.text(
            "SELECT c.canonical_name AS company, r.state, r.match_method, "
            "r.confidence, r.detected_at FROM eligibility_records r "
            "JOIN companies c ON c.id = r.company_id ORDER BY r.detected_at DESC"
        )
    ).mappings().all()
    events = conn.execute(
        sqlalchemy.text(
            "SELECT c.canonical_name AS company, e.type, e.status, e.deadline_at, "
            "e.start_at, e.current_payload->>'designation' AS designation, "
            "e.current_payload->>'salary_package' AS salary_package, "
            "e.current_payload->>'job_location' AS job_location, "
            "e.current_payload->>'source_message_id' AS source_message_id, "
            "e.current_payload->>'excerpt' AS excerpt FROM events e "
            "LEFT JOIN companies c ON c.id = e.company_id "
            "WHERE e.status IN ('DETECTED', 'ACTIVE') ORDER BY e.created_at DESC"
        )
    ).mappings().all()
    companies = conn.execute(
        sqlalchemy.text(
            "SELECT canonical_name, watch_state, lifecycle_stage FROM companies "
            "WHERE watch_state != 'NONE' ORDER BY canonical_name"
        )
    ).mappings().all()
    return {
        "eligibility": [dict(r) for r in eligibility],
        "events": [dict(r) for r in events],
        "watched_companies": [dict(r) for r in companies],
    }


def retrieve_context(
    conn: sqlalchemy.Connection, question: str, provider: NIMProvider
) -> dict:
    """Full retrieval for one question: message matches (vector + keyword,
    deduplicated by id) plus the structured facts block."""
    keyword = keyword_matches(conn, question)
    vector, vector_error = vector_matches(conn, question, provider)
    seen: set[str] = set()
    matches: list[dict] = []
    for row in vector + keyword:
        mid = str(row["id"])
        if mid in seen:
            continue
        seen.add(mid)
        matches.append(row)
    return {
        "question": question,
        "message_matches": matches,
        "vector_error": vector_error,
        "facts": structured_facts(conn),
    }
