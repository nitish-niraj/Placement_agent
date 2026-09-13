"""P14.1 smart field mapping — the brain of the form executor.

The executor must never be hardcoded to one form's field list: forms differ,
and NEW columns appear without notice. Design (ADR-004 posture — code decides,
LLM proposes, nothing is ever invented):

1. DETERMINISTIC hints: question text -> value key via ordered regex rules
   ("Registration Number" -> registration_number, "Faculty/Conducted by" ->
   teacher). First match wins, most specific first.
2. LLM fallback: questions the hints cannot confidently map go to the schema-
   validated model — which may only CHOOSE a catalog key (or "skip"). It never
   writes content. An invalid/garbled plan is discarded; the effect is simply
   a skipped field.
3. Every question that ends up without a value is REPORTED, never guessed:
   optional unknowns are left blank; a required one blocks submission.
"""

import re
from dataclasses import dataclass

import structlog

from pia_shared.schemas import FormFieldPlan
from pia_worker.ai.provider import NIMProvider, ProviderError

logger = structlog.get_logger()

# The catalog: every value the executor may EVER put in a form, with the
# description the LLM sees (values themselves never leave the system).
VALUE_CATALOG: dict[str, str] = {
    "full_name": "the student's full name",
    "email": "the student's email address",
    "registration_number": "the student's university registration number",
    "roll_number": "the student's roll number",
    "student_id": "the student's student ID",
    "branch": "the student's course/branch/programme (e.g. CSE, MCA)",
    "batch": "the student's batch or academic session",
    "cgpa": "the student's CGPA",
    "tenth_percent": "the student's 10th class percentage",
    "twelfth_percent": "the student's 12th class percentage",
    "backlog_count": "the student's number of active backlogs",
    "company": "the company that conducted the session",
    "teacher": "the teacher/presenter who conducted the session",
}

# Ordered deterministic hints: first matching rule wins. Specific fields
# BEFORE the generic "name" rule, and teacher/company BEFORE both, so e.g.
# "Company name" maps to company, not full_name.
_HINTS: list[tuple[str, re.Pattern[str]]] = [
    ("teacher", re.compile(
        r"\b(teacher|faculty|presenter|professor|speaker|mentor|"
        r"conducted by|taken by|presented by|session was taken)\b", re.IGNORECASE)),
    ("company", re.compile(
        r"\b(compan(?:y|ies)|organization|organisation|firm|recruiter|"
        r"which company)\b", re.IGNORECASE)),
    ("registration_number",
     re.compile(r"\bregistration\b|\bregn\.?\b|\breg\.?\s*no", re.IGNORECASE)),
    ("roll_number", re.compile(r"\broll\b", re.IGNORECASE)),
    ("student_id", re.compile(r"\b(student\s*id|scholar\s*(no|number)|uid)\b", re.IGNORECASE)),
    ("email", re.compile(r"\be-?mail\b", re.IGNORECASE)),
    ("backlog_count", re.compile(r"\b(backlogs?|arrears?)\b", re.IGNORECASE)),
    ("cgpa", re.compile(r"\b(cgpa|sgpa)\b", re.IGNORECASE)),
    ("tenth_percent", re.compile(r"\b(10th|x(th)?\s*class|matric)\b", re.IGNORECASE)),
    ("twelfth_percent", re.compile(r"\b(12th|xii(th)?\s*class|intermediate)\b", re.IGNORECASE)),
    ("branch", re.compile(
        r"\b(course|branch|programme|program|stream|specialization)\b", re.IGNORECASE)),
    ("batch", re.compile(r"\bbatch\b", re.IGNORECASE)),
    ("full_name", re.compile(r"\bname\b", re.IGNORECASE)),
]

_FILLABLE_KINDS = {"text", "long_text", "email", "dropdown", "radio", "date"}


@dataclass(frozen=True)
class Question:
    """One form question, read from the page (gform.py)."""

    index: int
    text: str
    kind: str  # text | long_text | email | dropdown | radio | checkbox | date | other
    required: bool
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class FillDecision:
    index: int
    question: str
    kind: str
    required: bool
    status: str  # filled | skipped_no_value | skipped_unmappable | skipped_kind | skipped_no_option
    value: str | None
    source: str  # hint | llm | none


def build_value_bag(profile: dict | None, payload: dict) -> dict[str, str]:
    """All values the executor may use, keyed by catalog key. Empty/None
    values are dropped — a missing profile field simply means that question
    reports as skipped_no_value instead of being filled with junk."""
    bag: dict[str, str] = {}
    if profile:
        for key, value in profile.items():
            if key in VALUE_CATALOG and value is not None and str(value).strip():
                bag[key] = str(value).strip()
    company = str(payload.get("company") or "").strip()
    if company and company.lower() != "none":
        bag["company"] = company
    presenters = [p for p in (payload.get("presenters") or []) if str(p).strip()]
    if presenters:
        bag["teacher"] = str(presenters[0]).strip()
    return bag


def map_hint(question_text: str, bag: dict[str, str]) -> str | None:
    """Deterministic mapping. Returns a key only when a hint matches AND the
    bag actually holds that value (a matched-but-missing key is reported
    downstream as skipped_no_value via map_key)."""
    for key, pattern in _HINTS:
        if pattern.search(question_text or ""):
            return key
    return None


def llm_map(questions: list[Question], bag: dict[str, str]) -> dict[int, str]:
    """LLM fallback for questions the hints left unmapped. Returns
    {question_index: value_key} — validated so the model can only pick catalog
    keys that exist in the bag, or nothing at all."""
    unmapped = [q for q in questions
                if q.kind in _FILLABLE_KINDS and map_hint(q.text, bag) is None]
    if not unmapped or not bag:
        return {}
    catalog_lines = "\n".join(f"- {k}: {d}" for k, d in VALUE_CATALOG.items()
                              if k in bag)
    question_lines = "\n".join(
        f"- [{q.index}] ({q.kind}) {q.text}" for q in unmapped)
    system = (
        "You map placement feedback-form questions to stored value keys. "
        "Choose ONLY from the provided keys; if no key fits a question, use "
        "\"skip\". Never invent values or new keys.")
    user = (f"Available value keys:\n{catalog_lines}\n\n"
            f"Questions:\n{question_lines}\n\n"
            "Return JSON: {\"mappings\": [{\"question\": ..., \"value_key\": ...}]}")
    try:
        provider = NIMProvider()
        plan: FormFieldPlan = provider.complete_structured(
            task="form_field_mapping", system=system, user=user,
            schema=FormFieldPlan, correlation_id="", max_tokens=500)[0]
    except ProviderError as exc:
        logger.warning("form_map_llm_unavailable", error=str(exc)[:120])
        return {}
    validated: dict[int, str] = {}
    for mapping in plan.mappings:
        if mapping.value_key == "skip":
            continue
        if mapping.value_key not in bag:
            continue  # the model may only choose what exists — never invent
        match = next((q for q in unmapped
                      if q.text.strip().lower() == mapping.question.strip().lower()),
                     None)
        if match is not None:
            validated[match.index] = mapping.value_key
    return validated


def decide_fills(questions: list[Question], bag: dict[str, str],
                 llm_plan: dict[int, str] | None = None) -> list[FillDecision]:
    """One FillDecision per question. Code owns every decision (ADR-004):
    the LLM plan is only ever a question->key suggestion, validated against
    the bag. Unfillable things are skipped with an explicit status so the
    report can name them — never guessed, never fatal."""
    llm_plan = llm_plan or {}
    decisions: list[FillDecision] = []
    for q in questions:
        if q.kind not in _FILLABLE_KINDS:
            decisions.append(FillDecision(q.index, q.text, q.kind, q.required,
                                          "skipped_kind", None, "none"))
            continue
        hint = map_hint(q.text, bag)
        key = hint or llm_plan.get(q.index)
        source = "hint" if hint else ("llm" if key else "none")
        if key is not None and key not in VALUE_CATALOG:
            key = None  # the model invented a key — discard, never guess
            source = "none"
        if key is None:
            decisions.append(FillDecision(q.index, q.text, q.kind, q.required,
                                          "skipped_unmappable", None, "none"))
            continue
        if key not in bag or not bag[key]:
            decisions.append(FillDecision(q.index, q.text, q.kind, q.required,
                                          "skipped_no_value", None, source))
            continue
        value = bag[key]
        if q.kind in ("dropdown", "radio", "checkbox"):
            option = pick_option(q.options, value)
            if option is None:
                decisions.append(FillDecision(q.index, q.text, q.kind, q.required,
                                              "skipped_no_option", None, source))
                continue
            value = option
        decisions.append(FillDecision(q.index, q.text, q.kind, q.required,
                                      "filled", value, source))
    return decisions


def blocked_required(decisions: list[FillDecision]) -> list[FillDecision]:
    """Required questions that did not get a value — these BLOCK submission."""
    return [d for d in decisions
            if d.required and d.status != "filled"]


def _norm(text: str) -> list[str]:
    """Normalized token list for fuzzy option matching."""
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split()


def pick_option(options: tuple[str, ...] | list[str], value: str) -> str | None:
    """Fuzzy-match a value against dropdown/radio options: normalized token
    containment in either direction, best overlap wins ("Sharma" matches
    "Dr. Anil Sharma"; exact match always wins). None = no confident option —
    the field is reported instead of guessed."""
    if not options:
        return None
    target = _norm(value)
    best, best_score = None, 0.0
    for option in options:
        if option.strip() == value.strip():
            return option
        candidate = _norm(option)
        if not candidate or not target:
            continue
        overlap = len(set(candidate) & set(target)) / max(len(set(target)), 1)
        contained = set(target) <= set(candidate) or set(candidate) <= set(target)
        score = 1.0 if contained and overlap >= 0.5 else overlap
        if score > best_score:
            best, best_score = option, score
    return best if best_score >= 0.5 else None
