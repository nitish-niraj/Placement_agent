"""F-019 deterministic event detection (FR-EVT-001) — pure functions.

Code owns the decision (ADR-004): event types come from fixed keyword rules,
NOT from the LLM. The entity extractor may enrich, but a missing/failed LLM
call must never lose an event — these rules alone are sufficient. Detection
order matters: the first matching pattern wins (most specific first).
"""

import re

from pia_shared.enums import EventType

_WS = re.compile(r"\s+")

# Ordered: first match wins. Specific/rare types before generic ones so e.g.
# "registration for the OA" classifies by the more informative signal.
EVENT_TYPE_PATTERNS: list[tuple[EventType, re.Pattern[str]]] = [
    (EventType.KYC, re.compile(r"\bkyc\b", re.IGNORECASE)),
    # VENUE is for venue-CHANGE announcements, not any message that mentions
    # one ("Reporting Venue: 29-402" is a reporting event with a venue detail).
    (EventType.VENUE,
     re.compile(r"\b(venue|location)\b.{0,40}\b(chang\w*|shift\w*|moved|revis\w*|"
                r"now)\b|\b(venue|location) (is now|updated)\b", re.IGNORECASE)),
    (EventType.SHORTLIST,
     re.compile(r"\bshortlist(ed|ing)?\b|\bselected for (the )?(interview|next round)\b",
                re.IGNORECASE)),
    (EventType.RESULT,
     re.compile(r"\bresults?\b.{0,30}\b(out|declared|announced)\b|"
                r"\bselected (students|candidates) (list|for)\b", re.IGNORECASE)),
    (EventType.JOINING,
     re.compile(r"\b(joining|onboarding|offer letter)\b", re.IGNORECASE)),
    (EventType.DOCUMENT_SUBMISSION,
     re.compile(r"\b(documents?|cv|resume|transcripts?|photographs?)\b.{0,40}\b"
                r"(submission|submit|upload|verification|carry|bring)\b|"
                r"\b(submit|upload)\b.{0,20}\b(documents?|cv|resume)\b",
                re.IGNORECASE)),
    (EventType.OA, re.compile(r"\bonline assessment\b|\boa\b", re.IGNORECASE)),
    (EventType.INTERVIEW,
     re.compile(r"\binterview\b|\breporting (schedule|date|time|venue)\b|"
                r"\bselection process\b|\bcampus drive\b|\bphysical process\b", re.IGNORECASE)),
    (EventType.EXAM,
     re.compile(r"\b(exam|examination|capp\d*|mid[- ]?sem|quiz|viva)\b", re.IGNORECASE)),
    (EventType.FORM,
     re.compile(r"\b(mandatory )?form\b.{0,40}\b(fill|complete|submit|submitted)\b|"
                r"\b(fill|complete|completes|submit)\b.{0,30}\bform\b", re.IGNORECASE)),
    (EventType.REGISTRATION,
     re.compile(r"\bregister\b|\bregistration\b", re.IGNORECASE)),
]

# Deadline-vs-occurrence context (FR-EVT-002): a date phrase next to deadline
# language becomes deadline_at; next to scheduling language becomes start_at.
DEADLINE_CONTEXT = re.compile(
    r"\b(last date|deadline|closes?|closed|close on|on or before|apply by|"
    r"register by|submission deadline|before|no later than|within)\b",
    re.IGNORECASE,
)
OCCURRENCE_CONTEXT = re.compile(
    r"\b(reporting date|reporting time|scheduled|schedule|on \d|declared|held|"
    r"date:|time:|venue:|at \d{1,2}[:.]?\d{0,2}\s*(am|pm))\b",
    re.IGNORECASE,
)

# LPU drive codes: "*COMPANY NAME* OC.41702.2027.63186" (also TC.) — mirrors
# the P5 file-name pattern (documents/orchestrator.company_from_file_name).
_DRIVE_CODE = re.compile(r"\b(?:OC|TC)\.\d{4,}", re.IGNORECASE)

# Lowercase connectors allowed INSIDE a company name; other lowercase words
# are prose and end the backward walk ("...shortlisted the selection process
# of MOVIDU TECHNOLOGY OC.x" must yield "MOVIDU TECHNOLOGY", not the sentence).
_NAME_CONNECTORS = {"of", "and", "the", "for", "&"}
_TOKEN = re.compile(r"^[A-Za-z0-9&.,'\-]+$")

VENUE_IN_TEXT = re.compile(
    r"\bvenue\s*[:\-]?\s*(?P<venue>[A-Za-z0-9][A-Za-z0-9 \-/\.]{0,30})",
    re.IGNORECASE,
)

_URL = re.compile(r"https?://\S+")


def detect_event_type(text: str) -> EventType | None:
    """First matching rule wins; None = no event signal in the text."""
    stripped = _WS.sub(" ", text or "").strip()
    if not stripped:
        return None
    for event_type, pattern in EVENT_TYPE_PATTERNS:
        if pattern.search(stripped):
            return event_type
    return None


def detect_deadline_context(text: str) -> bool:
    return bool(DEADLINE_CONTEXT.search(text or ""))


def detect_occurrence_context(text: str) -> bool:
    return bool(OCCURRENCE_CONTEXT.search(text or ""))


def extract_company_from_text(text: str) -> str | None:
    """Deterministic company signal from LPU drive codes in the message body:
    anchor on the code, then walk backward over name-like tokens (uppercase or
    connector words)."""
    flat = _WS.sub(" ", text or "")
    code = _DRIVE_CODE.search(flat)
    if code is None:
        return None
    collected: list[str] = []
    for token in reversed(flat[:code.start()].strip(" *-").split(" ")):
        core = token.strip(".,'*")
        if not core or not _TOKEN.match(core):
            break
        if core.islower() and core not in _NAME_CONNECTORS:
            break  # prose word — the name ended
        collected.append(core)
        if len(collected) >= 8:
            break
    while collected and collected[-1].lower() in _NAME_CONNECTORS:
        collected.pop()  # assembled name must not start with a connector
    name = " ".join(reversed(collected)).strip(" *")
    return name or None


_VENUE_NOISE = re.compile(
    r"\b(will be shared|shared soon|tbd|to be (decided|announced|informed)|later)\b",
    re.IGNORECASE,
)
# The body is whitespace-collapsed before matching, so a venue value can run
# into the next heading ("29-402 NOTE:") — cut there.
_VENUE_TRAIL = re.compile(r"\s+NOTE\b.*$|\s+VENUE\b.*$", re.IGNORECASE)


def extract_venue(text: str) -> str | None:
    match = VENUE_IN_TEXT.search(_WS.sub(" ", text or ""))
    if match is None:
        return None
    venue = _VENUE_TRAIL.sub("", match.group("venue")).strip(" .")
    if not venue or _VENUE_NOISE.search(venue):
        return None  # "Venue: will be shared soon" is not a venue
    return venue


def extract_links(text: str) -> list[str]:
    return list(dict.fromkeys(_URL.findall(text or "")))


# --- LPU drive-template labeled fields (role / package / location) --------------

# Real announcements carry "*Designation :-*", "*Salary Package :-*",
# "*Job Location :-*", "*Eligibility :-*" — the details that decide whether an
# opportunity is worth acting on. Parse them generically: value runs from the
# label marker to the next label marker (whitespace-collapsed body).
_FIELD_LABEL = re.compile(
    r"\*\s*(?P<label>Designation|Salary(?:\s+Package)?|Stipend|Job\s+Location|"
    r"Eligibility)\s*[:\-]+\s*\*?\s*",
    re.IGNORECASE,
)
_TRAILING_NOTE = re.compile(r"\s*+(?:IMPORTANT|NOTE)\b.*$", re.IGNORECASE)
_FIELD_KEY = {
    "designation": "designation",
    "salary": "salary_package",
    "salary package": "salary_package",
    "stipend": "salary_package",
    "job location": "job_location",
    "eligibility": "eligibility_note",
}


def extract_drive_fields(text: str) -> dict[str, str]:
    """Labeled drive-template fields -> {'designation': ..., 'salary_package': ...,
    'job_location': ..., 'eligibility_note': ...}. Never raises; missing labels
    simply yield nothing (FR-EVT-005 posture: nothing invented)."""
    flat = _WS.sub(" ", text or "").strip()
    matches = list(_FIELD_LABEL.finditer(flat))
    fields: dict[str, str] = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(flat)
        value = flat[match.end():end].strip()
        value = _TRAILING_NOTE.sub("", value).strip(" *-–:.")
        key = _FIELD_KEY.get(match.group("label").lower())
        if key and value:
            fields.setdefault(key, value[:200])
    return fields
