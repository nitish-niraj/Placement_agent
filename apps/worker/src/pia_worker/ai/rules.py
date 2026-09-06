"""Rule-based message classifier (FR-CLS-004, F-009).

Deterministic first pass: cheap patterns decide clear-cut cases and always act as
the override layer over LLM output (rules > rules-classifier > LLM). Pure functions
— unit-tested without a database or network.

Precedence: IGNORE > EXAMINATION > ADMINISTRATIVE > EVENT > PLACEMENT > ACADEMIC-prior > GENERAL.
Group category (owner-set) seeds the prior for ambiguous messages.
"""

import re

from pia_shared.enums import GroupCategory, Importance, MsgDomain

IGNORE_PATTERNS = re.compile(
    r"^(good\s*(morning|evening|night|afternoon))\b|^(thanks?|thank\s*you)\b|^(congrats|congratulations)\b"
    r"|^(happy\s+\w+)|^(ok|okay|done|yes|no|haan|nahi|sahi|theek)\b|^[👍🙏🎉😄😅❤️😊🔥\W]+$",
    re.IGNORECASE,
)
EXAMINATION_PATTERNS = re.compile(
    r"\b(exam|examination|ca[- ]?\d|capp?\d{2,4}|mid[- ]?sem|sem(ester)?\s*exam|quiz|viva|"
    r"test\s+syllabus|marks\s+(declared|posted)|result\s+(out|declared))\b",
    re.IGNORECASE,
)
ADMINISTRATIVE_PATTERNS = re.compile(
    r"\b(fee|fees|payment|installment|id\s*card|hostel|bus\s*route|circular|notice\b.*office|"
    r"document(s)?\s+(submission|verification)|registration\s*fee)\b",
    re.IGNORECASE,
)
EVENT_PATTERNS = re.compile(
    r"\b(hackathon|ideathon|workshop|webinar|bootcamp|seminar\b.*(session|talk)|tech\s*fest|"
    r"coding\s*(contest|competition)|meeting\s+link)\b",
    re.IGNORECASE,
)
PLACEMENT_PATTERNS = re.compile(
    r"\b(placement|drive|intern(ship)?|job\s*(opening|opportunity)|recruit|hiring|nqt|"
    r"(pre\s*)?(placement\s+)?training|shortlist|eligible\s+(candidates|students)|"
    r"oa\b|online\s*assessment|interview|ctc|package|lpa|offer\s+letter|"
    r"registration\s+(link|(is\s+)?open)|last\s+date|apply\s+(before|by|now)|"
    r"kyc\b|jd\b|job\s+description)\b",
    re.IGNORECASE,
)
DEADLINE_TODAY = re.compile(
    r"\b(today|tonight|by\s+today|closes?\s+today|today\s+\d{1,2}(:\d{2})?\s*(am|pm)|tomorrow|"
    r"within\s+\d+\s+(hours?|minutes?))\b",
    re.IGNORECASE,
)
URGENT_ACTION = re.compile(
    r"\b(registration|register|apply|submission|submit|deadline|last\s+date|"
    r"clos(e|ed|es|ing)\b|kyc\b|oa\b|interview|shortlist|eligible|"
    r"book\s+your\s+slot|link\s+expires?)\b",
    re.IGNORECASE,
)
TIME_OR_DATE = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}(:\d{2})?\s*(am|pm)|"
    r"\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)\b",
    re.IGNORECASE,
)
ACADEMIC_HINTS = re.compile(
    r"\b(cap\s*\d+|class|lecture|assignment|semester|unit\s*\d|syllabus|attendance|"
    r"section\s+[a-z]\b|lab\b|faculty)\b",
    re.IGNORECASE,
)


def classify_rules(
    text: str, group_category: GroupCategory | None = None
) -> tuple[MsgDomain, Importance]:
    """Deterministic (domain, importance) decision. Never throws on odd input."""
    stripped = (text or "").strip()
    if not stripped:
        return MsgDomain.UNKNOWN, Importance.IGNORE
    if IGNORE_PATTERNS.search(stripped) and len(stripped) < 80:
        return MsgDomain.GENERAL, Importance.IGNORE

    has_deadline_soon = bool(DEADLINE_TODAY.search(stripped))
    has_action = bool(URGENT_ACTION.search(stripped))

    if ADMINISTRATIVE_PATTERNS.search(stripped):  # fee/exam-fee before pure exams
        domain = MsgDomain.ADMINISTRATIVE
    elif EXAMINATION_PATTERNS.search(stripped):
        domain = MsgDomain.EXAMINATION
    elif EVENT_PATTERNS.search(stripped):
        domain = MsgDomain.EVENT
    elif PLACEMENT_PATTERNS.search(stripped):
        domain = MsgDomain.PLACEMENT
    elif ACADEMIC_HINTS.search(stripped):
        domain = MsgDomain.ACADEMIC
    elif group_category is GroupCategory.PLACEMENT:
        domain = MsgDomain.PLACEMENT
    elif group_category is GroupCategory.ACADEMIC:
        domain = MsgDomain.ACADEMIC
    elif group_category is GroupCategory.ADMINISTRATIVE:
        domain = MsgDomain.ADMINISTRATIVE
    else:
        domain = MsgDomain.GENERAL

    # Importance: domain-independent (FR-CLS-002) — driven by actionable signals,
    # not by domain alone. "Marks declared" is MEDIUM even though it's an exam topic.
    has_time_or_date = bool(TIME_OR_DATE.search(stripped))
    is_exam_with_date = domain is MsgDomain.EXAMINATION and has_time_or_date
    if has_deadline_soon and (has_action or is_exam_with_date):
        importance = Importance.CRITICAL
    elif has_action or is_exam_with_date:
        importance = Importance.HIGH
    else:
        importance = Importance.MEDIUM
    return domain, importance


def eligible_list_signal(text: str) -> bool:
    """Cheap FR-ELG-001 text signal: company-ish + eligible + names/roll context."""
    return bool(
        re.search(
            r"\b(eligible|shortlist(ed)?|selected\s+(candidates|students)|"
            r"candidates?\s+list|registered\s+students)\b",
            text or "",
            re.IGNORECASE,
        )
    )
