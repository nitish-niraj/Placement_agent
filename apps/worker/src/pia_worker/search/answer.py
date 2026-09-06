"""P12 conversational search (F-028) — answer composition + API-facing entry.

Two paths, hybrid per ADR-004:
- LLM: composes prose strictly from the retrieved context, schema-validated
  (ConversationalAnswer). Citations must reference provided context ids; the
  prompt forbids invention. Provider failure falls back cleanly.
- DETERMINISTIC fallback: when the LLM is unavailable, the endpoint returns the
  retrieved evidence list itself — source-backed, zero invention.

ADR-003: this is informational. Answers never mutate eligibility/events state.
"""

import sqlalchemy
import structlog

from pia_shared.schemas import (
    AnswerCitation,
    ConversationalAnswer,
)
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
- Answer ONLY from this material. Never invent companies, dates, roles, packages,
  or deadlines. If the material does not contain the answer, set says_unavailable
  to true and say exactly what is missing.
- Quote precise figures (deadline date/time in IST, stipend/CTC, role title)
  exactly as printed in the source. When the question asks about a package,
  salary, role, or designation, ALWAYS include the designation and
  salary_package fields of the relevant event verbatim.
- Every citation MUST be a WhatsApp message you actually used: kind "message",
  ref = that message's id from the MATCHING MESSAGES list. Never cite the
  structured facts block.
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


def ask_question(question: str) -> dict:
    """API entry: retrieval -> LLM answer (or deterministic evidence fallback).
    Returns {answer, citations, says_unavailable, confidence, fallback}."""
    provider = NIMProvider()
    engine = _engine()
    with engine.connect() as conn:
        context = retrieve_context(conn, question, provider)

    try:
        answer: ConversationalAnswer = provider.complete_structured(
            task="conversational_answer",
            system=ANSWER_SYSTEM,
            user=_context_block(context),
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
        }
    except ProviderError as exc:
        logger.warning("ask_llm_unavailable", error=str(exc)[:120])
        fallback_citations: list[dict] = []
        for m in context["message_matches"][:6]:
            citation = AnswerCitation(kind="message", ref=str(m["id"]),
                                      quote=(m.get("text") or "")[:160])
            fallback_citations.append(citation.model_dump(mode="json"))
        facts = context["facts"]
        eligible_names = [
            str(r["company"]) for r in facts["eligibility"]
            if r["state"] in ("ELIGIBLE", "USER_CONFIRMED")
        ]
        return {
            "answer": (
                "The language model is unavailable right now — here is what the "
                f"stored data shows directly: eligible companies: "
                f"{', '.join(eligible_names) or 'none recorded'}; "
                f"{len(facts['events'])} active event(s). "
                "Citations below are the closest matching messages."
            ),
            "citations": fallback_citations,
            "says_unavailable": False,
            "confidence": 0.6,
            "fallback": True,
        }


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
