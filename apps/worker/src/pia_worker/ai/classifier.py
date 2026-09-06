"""Merged classifier: rules first, LLM second, rules win on conflict (FR-CLS-004).

- If the deterministic classifier is confident (ignore/high/critical signals or a
  clear domain pattern), the LLM is skipped entirely — cheaper and calmer.
- Otherwise the LLM classifies with a few-shot prompt; its output is schema-
  validated (ADR-004). If the LLM result conflicts with rules, rules win
  (override precedence), and the conflict is recorded in the payload.
- Provider failures never fail the pipeline: caller receives a rule-based result
  with confidence=0.0 and source="rules_fallback".
"""

import re

import structlog

from pia_shared.enums import GroupCategory, Importance, MsgDomain
from pia_shared.schemas import Classification, Entities
from pia_worker.ai.provider import NIMProvider, ProviderError
from pia_worker.ai.rules import classify_rules
from pia_worker.settings import get_settings

logger = structlog.get_logger()

SYSTEM_PROMPT = """You classify WhatsApp messages for a student's placement assistant.
Domain meanings: PLACEMENT=company drives, internships, job openings, OA/interviews,
eligibility lists. ACADEMIC=course/class/assignment notices. EXAMINATION=exams, CA tests,
results, marks. ADMINISTRATIVE=fees, ID cards, document submission, office notices.
EVENT=hackathons, workshops, webinars. GENERAL=chatter, greetings. UNKNOWN=unclear.
Importance: CRITICAL=deadline is today/tonight or action required immediately.
HIGH=deadlines/dates given, registration open, shortlists, eligible lists.
MEDIUM=informational notice. LOW=contextual info. IGNORE=greetings, thanks, memes, chatter.
Calibration: a notice with NO deadline and NO required action is MEDIUM even when it is
about exams, fees, or classes (e.g. "marks declared", "fee window is open", "lecture
rescheduled to tomorrow" are all MEDIUM).
Respond with ONLY a JSON object:
{"domain": "<PLACEMENT|ACADEMIC|EXAMINATION|ADMINISTRATIVE|EVENT|GENERAL|UNKNOWN>",
 "importance": "<CRITICAL|HIGH|MEDIUM|LOW|IGNORE>", "confidence": <0-1>,
 "rationale": "<one sentence>"}
Do not invent facts. Examples:
{"domain":"PLACEMENT","importance":"CRITICAL","confidence":0.95,
 "rationale":"OA tomorrow, reg closes today"}
{"domain":"GENERAL","importance":"IGNORE","confidence":0.98,"rationale":"greeting"}
{"domain":"EXAMINATION","importance":"MEDIUM","confidence":0.9,
 "rationale":"marks declared, informational only"}
{"domain":"ACADEMIC","importance":"MEDIUM","confidence":0.9,
 "rationale":"timetable change, no action required"}"""

# Rule results we trust without paying for an LLM call.
_STRONG_IMPORTANCE = {Importance.IGNORE, Importance.CRITICAL}


def classify_message(
    text: str,
    group_category: str | None = None,
    provider: NIMProvider | None = None,
    correlation_id: str = "",
) -> Classification:
    settings = get_settings()
    category = GroupCategory(group_category) if group_category else None
    rule_domain, rule_importance = classify_rules(text, category)

    if rule_importance in _STRONG_IMPORTANCE or settings.nvidia_api_key == "":
        return Classification(
            domain=rule_domain,
            importance=rule_importance,
            confidence=0.9 if rule_importance in _STRONG_IMPORTANCE else 0.6,
            rationale="deterministic rules (FR-CLS-004)"
            if rule_importance in _STRONG_IMPORTANCE
            else "rules fallback (no LLM key configured)",
        )

    provider = provider or NIMProvider()
    try:
        llm: Classification = provider.complete_structured(
            task="message_classifier",
            system=SYSTEM_PROMPT,
            user=text[:4000],
            schema=Classification,
            correlation_id=correlation_id,
        )[0]
    except ProviderError as exc:
        logger.warning("llm_classification_failed", error=str(exc)[:120])
        return Classification(
            domain=rule_domain, importance=rule_importance, confidence=0.0,
            rationale="rules fallback (LLM unavailable)",
        )

    # Override precedence (FR-CLS-004): deterministic rules win on conflict.
    if llm.importance != rule_importance and rule_importance in _STRONG_IMPORTANCE:
        return Classification(
            domain=llm.domain if rule_domain is MsgDomain.GENERAL else rule_domain,
            importance=rule_importance,
            confidence=llm.confidence,
            rationale=f"rules override LLM: {llm.rationale}",
        )
    # Domain conflict: prefer the more specific rule domain when rules saw a pattern.
    if rule_domain is not MsgDomain.GENERAL and llm.domain != rule_domain:
        return Classification(
            domain=rule_domain, importance=llm.importance, confidence=llm.confidence,
            rationale=f"rules override LLM domain: {llm.rationale}",
        )
    return llm


ENTITY_PROMPT = """You extract entities from a WhatsApp message for a placement assistant.
Extract ONLY what is literally present in the text — never invent or resolve values.
- companies: company/organisation names mentioned (e.g. drive announcements)
- dates: every date/time phrase exactly as written ("raw"); set "relative_anchor"
  for words like today/tomorrow; leave "resolved_at" null
- links: URLs present in the text
- actions: required user actions (register, apply, submit form, attend, pay)
- locations: venues/locations, if stated
- document_refs: referenced attachments/documents, if named
Empty lists are correct when nothing applies. Respond with ONLY JSON:
{"companies": [{"name": "...", "alias_of": null}],
 "dates": [{"raw": "...", "resolved_at": null, "relative_anchor": null}],
 "links": ["..."], "locations": ["..."],
 "actions": [{"type": "register", "description": null}],
 "document_refs": []}"""

_URL = re.compile(r"https?://\S+")


def extract_entities(
    text: str,
    provider: NIMProvider | None = None,
    correlation_id: str = "",
) -> dict:
    """FR-CLS-003: links are deterministic (regex); the rest via schema-validated LLM.
    Returns a plain dict (JSON-ready). Never raises — callers merge the result into
    the classification payload and LLM absence just means fewer fields."""
    entities = {
        "companies": [], "dates": [], "locations": [], "actions": [],
        "document_refs": [],
        "links": list(dict.fromkeys(_URL.findall(text or ""))),  # dedupe, keep order
    }
    if get_settings().nvidia_api_key == "":
        return entities
    provider = provider or NIMProvider()
    try:
        llm: Entities = provider.complete_structured(
            task="entity_extractor",
            system=ENTITY_PROMPT,
            user=text[:4000],
            schema=Entities,
            correlation_id=correlation_id,
        )[0]
    except ProviderError as exc:
        logger.warning("entity_extraction_failed", error=str(exc)[:120])
        return entities
    entities["companies"] = [c.model_dump() for c in llm.companies]
    entities["dates"] = [d.model_dump() for d in llm.dates]
    entities["locations"] = llm.locations
    entities["actions"] = [a.model_dump() for a in llm.actions]
    entities["document_refs"] = llm.document_refs
    return entities
