"""Message topics — what a placement message is ABOUT (worker-side, pure).

MsgDomain/EventType are frozen Postgres enums; topics are a pure-Python layer
for routing and rendering that needs no migration. Order matters: specific
stages win over generic ones ("not registered" is a DEFAULTER_CHECK, not a
REGISTRATION opening; a feedback form is a SURVEY, not a FORM event).

Bare words like "test" never match anything here — an "online test" must say
so explicitly, otherwise OA announcements would die as system noise.
"""

import re
from enum import StrEnum


class MessageTopic(StrEnum):
    SYSTEM_TEST = "SYSTEM_TEST"
    DEFAULTER_CHECK = "DEFAULTER_CHECK"
    SURVEY = "SURVEY"
    SHORTLIST = "SHORTLIST"
    INTERVIEW = "INTERVIEW"
    ONLINE_TEST = "ONLINE_TEST"
    ASSESSMENT = "ASSESSMENT"
    DOCUMENT_SUBMISSION = "DOCUMENT_SUBMISSION"
    REGISTRATION = "REGISTRATION"
    TRAINING_SESSION = "TRAINING_SESSION"
    PLACEMENT_OPPORTUNITY = "PLACEMENT_OPPORTUNITY"
    ADMINISTRATIVE = "ADMINISTRATIVE"
    INFORMATIONAL = "INFORMATIONAL"
    UNKNOWN = "UNKNOWN"


# Ops/test chatter that must never reach the student pipeline. Deliberately
# multi-word or hyphenated markers — a bare "test" is a real assessment word.
_SYSTEM_TEST_RE = re.compile(
    r"\b(alert[-_ ]?test|watcher([-_ ]?(self)?[-_ ]?test|fmt[-_ ]?test)?|"
    r"self[-_ ]?test|dry[-_ ]?run|health[-_ ]?check|test\s+alert|"
    r"testing\s+(the\s+)?(bot|watcher|pipeline|notification)|"
    r"ignore\s+this|dummy\s+(message|data)|trial\s+run|debug\s+message)\b",
    re.IGNORECASE,
)

_DEFAULTER_RE = re.compile(
    r"\bdefaulter(s| list)?\b|\bnot\s+(yet\s+)?registered\b|"
    r"\byet\s+to\s+register\b|\bunregistered\b|\bpending\s+registration\b|"
    r"\bregistration\s+(pending|incomplete)\b",
    re.IGNORECASE,
)

_SURVEY_RE = re.compile(
    r"\bsurvey\b|\bpoll\b|\bfeedback\s+form\b|\bfill\b.{0,30}\bfeedback\b|"
    r"\bfeedback\b.{0,30}\bform\b",
    re.IGNORECASE,
)

_SHORTLIST_RE = re.compile(
    r"\bshortlist(ed|ing)?\b|\bselected\s+(for|students|candidates)|"
    r"\bprovisional\b.{0,20}\blist\b|\bmerit\s+list\b",
    re.IGNORECASE,
)

_INTERVIEW_RE = re.compile(
    r"\binterview\b|\breporting\s+(schedule|date|time|venue)\b|"
    r"\bselection\s+process\b|\bcampus\s+drive\b|\bphysical\s+process\b",
    re.IGNORECASE,
)

_ONLINE_TEST_RE = re.compile(
    r"\bonline\s+assessment\b|\boa\b|\baptitude\b|\bamcat\b|\bcocubes\b|"
    r"\btest\s+(link|credentials|id|password|slot|schedule)\b|"
    r"\bslot\s+booking\b",
    re.IGNORECASE,
)

_ASSESSMENT_RE = re.compile(
    r"\bassessment\b|\bcoding\s+(round|test|challenge)\b|"
    r"\btechnical\s+(round|test)\b|\bgroup\s+discussion\b|\bgd\b.{0,10}\bround\b",
    re.IGNORECASE,
)

_DOCUMENT_RE = re.compile(
    r"\bdocuments?\b.{0,40}\b(submi\w+|upload|verif\w+|carry|bring)\b|"
    r"\b(submit|upload|carry|bring)\b.{0,20}\b(documents?|cv|resume|"
    r"transcripts?|certificates?|photographs?)\b",
    re.IGNORECASE,
)

_REGISTRATION_RE = re.compile(
    r"\bregist(er|ration|ered\b.{0,20}\b(link|open|portal))\b|"
    r"\bregistration\b.{0,20}\b(link|open|portal|started)\b|"
    r"\benroll(ment|ed)?\b|\bapply\s+(by|before|now)\b|\blast\s+date\b|"
    r"\bfill\b.{0,30}\bform\b|\bkyc\b",
    re.IGNORECASE,
)

_TRAINING_RE = re.compile(
    r"\btraining\s+session\b|\bworkshop\b|\bwebinar\b|\bbootcamp\b|"
    r"\bhackathon\b|\bideathon\b|\bskill\s+(development|training)\b",
    re.IGNORECASE,
)

_OPPORTUNITY_RE = re.compile(
    r"\bhiring\b|\bopening\b|\bvacan\w+\b|\brecruit\w*\b|\bdrive\b|"
    r"\beligib(le|ility)\b|\bintern(ship)?\b|\bjob\b|\bctc\b|\blpa\b|"
    r"\boffer\s+letter\b|\bjoining\b",
    re.IGNORECASE,
)

_ADMIN_RE = re.compile(
    r"\bfee(s)?\b|\bpayment\b|\bhostel\b|\bbus\s+route\b|\bid\s*card\b|"
    r"\bcircular\b|\bnotice\b.{0,20}\boffice\b",
    re.IGNORECASE,
)


def is_system_test(text: str) -> bool:
    """True for watcher/dry-run/health-check chatter. Never matches bare
    "test" — assessment language must survive this filter."""
    return bool(_SYSTEM_TEST_RE.search(text or ""))


def classify_topic(text: str) -> MessageTopic:
    """One topic per message, first match wins (most specific first)."""
    body = text or ""
    if is_system_test(body):
        return MessageTopic.SYSTEM_TEST
    if _DEFAULTER_RE.search(body):
        return MessageTopic.DEFAULTER_CHECK
    if _SURVEY_RE.search(body):
        return MessageTopic.SURVEY
    if _SHORTLIST_RE.search(body):
        return MessageTopic.SHORTLIST
    if _INTERVIEW_RE.search(body):
        return MessageTopic.INTERVIEW
    if _ONLINE_TEST_RE.search(body):
        return MessageTopic.ONLINE_TEST
    if _ASSESSMENT_RE.search(body):
        return MessageTopic.ASSESSMENT
    if _DOCUMENT_RE.search(body):
        return MessageTopic.DOCUMENT_SUBMISSION
    if _REGISTRATION_RE.search(body):
        return MessageTopic.REGISTRATION
    if _TRAINING_RE.search(body):
        return MessageTopic.TRAINING_SESSION
    if _OPPORTUNITY_RE.search(body):
        return MessageTopic.PLACEMENT_OPPORTUNITY
    if _ADMIN_RE.search(body):
        return MessageTopic.ADMINISTRATIVE
    if body.strip():
        return MessageTopic.INFORMATIONAL
    return MessageTopic.UNKNOWN
