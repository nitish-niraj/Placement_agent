"""P12 conversational search (F-028) — answer composition + API-facing entry.

Ladder per DEC-010 (extended to Ask 2026-09-13), hybrid per ADR-004:
- NIM: composes prose strictly from the retrieved context, schema-validated
  (ConversationalAnswer). Citations must reference provided context ids; the
  prompt forbids invention.
- OpenRouter / Groq: plain-text brains when NIM is down; citations come from
  retrieval itself (messages actually retrieved — never invented).
- DETERMINISTIC: when every LLM rung fails, the endpoint returns the retrieved
  evidence itself — source-backed, zero invention, question-aware where the
  stored records support it (e.g. "latest eligible company" by detected_at).

The `source` field on the result names the rung that answered. ADR-003: this
is informational. Answers never mutate eligibility/events state."""

import sqlalchemy
import structlog

from pia_shared.schemas import (
    AnswerCitation,
    ConversationalAnswer,
)
from pia_worker.ai.fallback import groq_chat, openrouter_chat
from pia_worker.ai.provider import NIMProvider, ProviderError
from pia_worker.jobs.process_message import _engine
from pia_worker.search.retrieval import retrieve_context

logger = structlog.get_logger()

_MAX_CONTEXT_CHARS = 9000

ANSWER_SYSTEM = """You answer questions about the user's placement intelligence data.
You receive: (1) stored structured facts (eligibility records, active events with
role/package/location, watched companies) and (2) matching WhatsApp messages.
Rules:
- Answer THE EXACT question asked. When the question names a company, use only
  that company's records — other companies in the context are noise.
- The STRUCTURED FACTS block is authoritative and sufficient on its own: when
  it answers the question, answer from it directly — never claim the data is
  missing while the facts block contains the answer, and never refuse merely
  because the MATCHING MESSAGES list is empty.
- Answer ONLY from this material. Never invent companies, dates, roles, packages,
  or deadlines. If the material does not contain the answer, set says_unavailable
  to true and say exactly what is missing.
- Quote precise figures (deadline date/time in IST, stipend/CTC, role title)
  exactly as printed in the source. When the question asks about a package,
  salary, role, or designation, ALWAYS include the designation and
  salary_package fields of the relevant event verbatim.
- Citations: every citation MUST reference material you actually used, kind
  "message" with ref = that message's id from the MATCHING MESSAGES list. If
  you used only the structured facts, return an empty citations list — do not
  invent refs.
- Keep the answer under 120 words. Plain text, no markdown headers."""


def _context_block(context: dict) -> str:
    facts = context["facts"]
    lines = ["STRUCTURED FACTS (authoritative):"]
    for r in facts["eligibility"]:
        lines.append(
            f"- eligibility: {r['company']} = {r['state']} "
            f"(matched via {r['match_method'] or 'n/a'}, detected {str(r['detected_at'])[:10]})"
        )
    for c in facts["watched_companies"]:
        stage = c['lifecycle_stage'] or 'tracked'
        lines.append(f"- watched company: {c['canonical_name']} ({stage})")
    for e in facts["events"]:
        detail = ", ".join(
            f"{k}: {e[k]}" for k in
            ("designation", "salary_package", "job_location", "deadline_at", "start_at")
            if e.get(k)
        )
        lines.append(
            f"- event: {e['company'] or 'General'} {e['type']} ({e['status']})"
            + (f" — {detail}" if detail else "")
        )
    lines.append("\nMATCHING MESSAGES (observed source text):")
    for m in context["message_matches"][:10]:
        text = (m.get("text") or "").replace("\n", " ")[:400]
        lines.append(f"- [{m['id']}] {m.get('group_name')}: {text}")
    block = "\n".join(lines)
    return block[:_MAX_CONTEXT_CHARS]


_FACT_TRIGGERS = ("package", "salary", "stipend", "ctc", "role", "designation",
                  "location", "lpa")
# Companies are matched by distinctive name tokens ("techademy" matches
# "TECHADEMY LEARNING SOLUTIONS PVT. LTD.") — questions use short names.
_COMPANY_STOP = {"pvt", "ltd", "limited", "group", "india", "solutions",
                 "technologies", "of", "companies"}

# Deterministic-run only: questions whose stored-records answer is a simple
# ordering fact (eligibility rows arrive newest-first by detected_at).
_LATEST_TRIGGERS = ("latest", "most recent", "newest", "last company", "recent")


def _company_matches(company: str, lowered_question: str) -> bool:
    from pia_shared.textnorm import normalize_name

    if company in lowered_question:
        return True
    tokens = normalize_name(company).split()
    distinctive = [t for t in tokens if len(t) >= 5 and t not in _COMPANY_STOP]
    return any(token in lowered_question for token in distinctive)


def _augment_with_drive_facts(question: str, context: dict, answer_text: str) -> str:
    """Code owns facts (ADR-004): when the question asks about package/role/
    location, append the stored event fields verbatim — the small hosted model
    sometimes under-quotes them, and this answer line is never allowed to be
    missing. Skips literal 'NA' values (printed in some announcements)."""
    lowered = question.lower()
    if not any(trigger in lowered for trigger in _FACT_TRIGGERS):
        return answer_text
    lines: list[str] = []
    labels = (("designation", "Role"), ("salary_package", "Package"),
              ("job_location", "Location"))
    matched_event_id: str | None = None
    for event in context["facts"]["events"]:
        company = (event.get("company") or "").lower()
        if not company or not _company_matches(company, lowered):
            continue
        for field, label in labels:
            value = event.get(field)
            if value and value.strip().upper() != "NA":
                lines.append(f"{label}: {value}")
        if lines:
            matched_event_id = event.get("source_message_id")
    if not lines:
        return answer_text
    answer_text = answer_text + "\n\nFrom the stored record:\n" + "\n".join(
        f"• {line}" for line in lines)
    if matched_event_id:
        answer_text += f"\n(Source message: {matched_event_id})"
    return answer_text


def _retrieval_citations(context: dict, limit: int = 6) -> list[dict]:
    """Citations for rungs that cannot cite themselves: the top retrieved
    messages — actually retrieved, so never invented."""
    citations: list[dict] = []
    for m in context["message_matches"][:limit]:
        citation = AnswerCitation(kind="message", ref=str(m["id"]),
                                  quote=(m.get("text") or "")[:160])
        citations.append(citation.model_dump(mode="json"))
    return citations


def _eligible_with_dates(context: dict) -> list[tuple[str, str]]:
    """(company, detected date) for ELIGIBLE/USER_CONFIRMED rows, newest first
    (structured_facts orders by detected_at DESC)."""
    return [
        (str(r["company"]), str(r.get("detected_at") or "")[:10])
        for r in context["facts"]["eligibility"]
        if r["state"] in ("ELIGIBLE", "USER_CONFIRMED")
    ]


def _deterministic_answer(question: str, context: dict) -> dict:
    """Final rung: composed from stored records only, question-aware where the
    data supports it ('latest' = newest detected_at; never a guess)."""
    facts = context["facts"]
    eligible = _eligible_with_dates(context)
    lowered = question.lower()
    if any(trigger in lowered for trigger in _LATEST_TRIGGERS) and eligible:
        name, date = eligible[0]
        answer = f"Latest company you became eligible for: {name} (detected {date})."
        if len(eligible) > 1:
            answer += (" Earlier eligible companies, newest first: "
                       + ", ".join(f"{n} ({d})" for n, d in eligible[1:6]) + ".")
    else:
        listing = ", ".join(f"{n} ({d})" for n, d in eligible) or "none recorded"
        answer = (
            "The language model is unavailable right now — here is what the "
            "stored data shows directly: eligible companies (newest first): "
            f"{listing}; {len(facts['events'])} active event(s). "
            "Citations below are the closest matching messages."
        )
    return {
        "answer": answer,
        "citations": _retrieval_citations(context),
        "says_unavailable": False,
        "confidence": 0.6,
        "fallback": True,
        "source": "deterministic",
    }


def ask_question(question: str) -> dict:
    """API entry: retrieval -> DEC-010 answer ladder -> deterministic evidence.
    Rungs: NIM structured -> OpenRouter plain text -> Groq plain text ->
    deterministic. Returns {answer, citations, says_unavailable, confidence,
    fallback, source}."""
    provider = NIMProvider()
    engine = _engine()
    with engine.connect() as conn:
        context = retrieve_context(conn, question, provider)

    # The question MUST travel with the context: the model otherwise only sees
    # facts and honestly replies "no question was provided" (live bug).
    user_prompt = f"QUESTION: {question}\n\n{_context_block(context)}"

    nim_error = ""
    try:
        answer: ConversationalAnswer = provider.complete_structured(
            task="conversational_answer",
            system=ANSWER_SYSTEM,
            user=user_prompt,
            schema=ConversationalAnswer,
            correlation_id="",
            max_tokens=400,
        )[0]
        known_ids = {str(m["id"]) for m in context["message_matches"]}
        citations = [c for c in answer.citations if str(c.ref) in known_ids]
        return {
            "answer": _augment_with_drive_facts(question, context, answer.answer),
            "citations": [c.model_dump(mode="json") for c in citations],
            "says_unavailable": answer.says_unavailable,
            "confidence": answer.confidence,
            "fallback": False,
            "source": "nim",
        }
    except ProviderError as exc:
        nim_error = str(exc)
        logger.warning("ask_llm_unavailable", rung="nim", error=nim_error[:120])

    for rung in ("openrouter", "groq"):
        chat = openrouter_chat if rung == "openrouter" else groq_chat
        try:
            # 1500, not ~400: the free-tier reasoning models spend the budget
            # on their thinking block first — a tight cap yields an empty
            # content (same live finding as the listener's summary rungs).
            text = chat(ANSWER_SYSTEM, user_prompt, max_tokens=1500)
        except ProviderError as rung_exc:
            logger.warning("ask_llm_unavailable", rung=rung,
                           error=str(rung_exc)[:120])
            continue
        return {
            "answer": _augment_with_drive_facts(question, context, text),
            "citations": _retrieval_citations(context),
            "says_unavailable": False,
            "confidence": 0.55,
            "fallback": False,
            "source": rung,
        }

    logger.warning("ask_deterministic_fallback", nim_error=nim_error[:120])
    return _deterministic_answer(question, context)


def embedding_backfill_progress(conn: sqlalchemy.Connection) -> dict:
    """How many enable-group messages still lack embeddings (observability)."""
    row = conn.execute(
        sqlalchemy.text(
            "SELECT count(*) AS total, "
            "(SELECT count(*) FROM message_embeddings) AS embedded FROM messages m "
            "JOIN groups g ON g.id = m.group_id "
            "WHERE m.text IS NOT NULL AND g.enabled"
        )
    ).mappings().first()
    return dict(row) if row else {}
