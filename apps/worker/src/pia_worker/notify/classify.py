"""Application-aware message classification — company mentioned != applied.

A company/role mention alone must never become a post-application follow-up.
This module answers two pure questions about a message's TEXT:

1. `analyze_application_message(text)` — what KIND of message is this?
   (pre-application opening, application-related update, post-application
   action required, or generic noise) + does it demand an action?
2. `decide_application_followup(...)` — given the student's stored
   ApplicationStatus for the matched company+role, should this message
   become a follow-up, an ask-whether-applied nudge, or nothing?

Deterministic keyword rules (ADR-004 posture): the LLM never decides
follow-ups. Role/company matching stays with the caller — `role_matched`
must already reflect company + normalized-role agreement.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from pia_shared.enums import (
    ApplicationStatus,
    EventType,
    NotificationPriority,
)


class ApplicationMessageType(StrEnum):
    PRE_APPLICATION = "PRE_APPLICATION"
    APPLICATION_RELATED = "APPLICATION_RELATED"
    POST_APPLICATION_ACTION = "POST_APPLICATION_ACTION"
    GENERIC = "GENERIC"


@dataclass(frozen=True)
class MessageAnalysis:
    message_type: ApplicationMessageType
    requires_action: bool
    has_consequence: bool
    priority: NotificationPriority


@dataclass(frozen=True)
class FollowUpDecision:
    should_follow_up: bool
    suggest_ask_applied: bool
    reason: str
    priority: NotificationPriority


# Consequence language: cancellation/rejection risk — always HIGH.
_CONSEQUENCE_RE = re.compile(
    r"\b(cancel(l|led|lation)?|reject(ed|ion)?|fail(ure|ed)?|debar|blacklist|"
    r"disqualif\w*|will not be considered|no longer (eligible|considered)|"
    r"last chance)\b",
    re.IGNORECASE,
)

# Action verbs: the message asks the reader to DO something verifiable.
_ACTION_RE = re.compile(
    r"\b(verify|verification|confirm(ation)?|complete|submit\w*|upload|fill|"
    r"check (your|their)|otp|correct(ion)?|report\b.{0,20}\b(by|on|at)\b|"
    r"appear for|attend\b.{0,20}\b(assessment|interview|test)\b|"
    r"assessment|slot booking|book your slot)\b",
    re.IGNORECASE,
)

# Application-process nouns: ties the message to an (alleged) application.
_APPLICATION_RE = re.compile(
    r"\b(appl(ied|ication|y(ing)?)|regist(ered|ration|er)|enroll(ed|ment)?|"
    r"confirmation\b.{0,20}\bemail\b|form\b.{0,20}\bsubmi\w*|"
    r"portal|credentials|login|shortlist(ed)?|offer letter|joining)\b",
    re.IGNORECASE,
)

# Pre-application openings: opportunities the student may not have touched.
_PRE_APPLICATION_RE = re.compile(
    r"\b(hiring|opening|announced|vacan\w+|opportunity|opportunities|"
    r"invites? applications?|drive\b|eligib(le|ility)|apply by|register by|"
    r"last date.{0,20}apply|has opened applications?|released a .*"
    r"(opportunity|opening))\b",
    re.IGNORECASE,
)

# Generic noise: experiences, news, discussion — never a follow-up.
_GENERIC_RE = re.compile(
    r"\b(interview experience|my experience|salary\b.{0,20}(discuss|expect)|"
    r"\bnews\b|all the best|congratulations|doubts? (regarding|about)|"
    r"any(one|body).{0,20}(know|idea)|thoughts on)\b",
    re.IGNORECASE,
)


def analyze_application_message(text: str) -> MessageAnalysis:
    """Classify one message's text. Order matters: consequence/action signals
    win over opening language ("hiring ... must complete verification" is a
    post-application action, not an opening)."""
    body = text or ""
    has_consequence = bool(_CONSEQUENCE_RE.search(body))
    requires_action = bool(_ACTION_RE.search(body))
    mentions_application = bool(_APPLICATION_RE.search(body))

    actionable = requires_action or mentions_application
    if (requires_action and mentions_application
            or has_consequence and actionable):
        message_type = ApplicationMessageType.POST_APPLICATION_ACTION
    elif mentions_application:
        message_type = ApplicationMessageType.APPLICATION_RELATED
    elif _GENERIC_RE.search(body):
        message_type = ApplicationMessageType.GENERIC
    elif _PRE_APPLICATION_RE.search(body):
        message_type = ApplicationMessageType.PRE_APPLICATION
    elif requires_action:
        # An imperative without application nouns ("report at 9 AM") is still
        # action-shaped — treat as post-application action, low confidence.
        message_type = ApplicationMessageType.POST_APPLICATION_ACTION
    else:
        message_type = ApplicationMessageType.GENERIC

    if message_type is ApplicationMessageType.POST_APPLICATION_ACTION:
        priority = (NotificationPriority.HIGH if has_consequence
                    else NotificationPriority.MEDIUM)
    elif (message_type is ApplicationMessageType.APPLICATION_RELATED
            and requires_action):
        priority = NotificationPriority.MEDIUM
    else:
        priority = NotificationPriority.LOW
    return MessageAnalysis(
        message_type=message_type, requires_action=requires_action,
        has_consequence=has_consequence, priority=priority)


# Event types whose notifications may become post-application follow-ups —
# the application-state gate applies to these only. EXAM/VENUE/OTHER stay on
# the legacy ladder (academic/venue announcements surface regardless).
APPLICATION_GATED_TYPES = frozenset({
    EventType.FORM, EventType.KYC, EventType.REGISTRATION,
    EventType.DOCUMENT_SUBMISSION, EventType.OA, EventType.INTERVIEW,
    EventType.SHORTLIST, EventType.JOINING, EventType.RESULT,
})

# Event types suppressed by the strict list gate: a candidate-list attachment
# (CSV/XLSX/PDF/image, including split-message bundles) that does NOT contain
# the student kills these — Mars.AI-style spam AND false SHORTLIST hallucinations
# (text says "shortlisted" but the attached list lacks the student) end here.
LIST_GATED_TYPES = frozenset({
    EventType.FORM, EventType.KYC, EventType.REGISTRATION,
    EventType.SHORTLIST, EventType.RESULT, EventType.OA, EventType.INTERVIEW})


def is_post_application_shaped(analysis: MessageAnalysis) -> bool:
    """True when the message reads like a post-application update (action or
    confirmation tied to an application) rather than an opening or noise."""
    return (
        analysis.message_type is ApplicationMessageType.POST_APPLICATION_ACTION
        or (analysis.message_type
            is ApplicationMessageType.APPLICATION_RELATED
            and analysis.requires_action))
# Statuses where the student has answered: never ask again, never assume.
_ANSWERED_NEGATIVE = frozenset({
    ApplicationStatus.NOT_APPLIED, ApplicationStatus.NOT_INTERESTED})
_UNDECIDED = frozenset({
    ApplicationStatus.UNKNOWN, ApplicationStatus.NOT_SURE,
    ApplicationStatus.ELIGIBLE_NOT_APPLIED})


def decide_application_followup(
    *, status: ApplicationStatus, analysis: MessageAnalysis,
    role_matched: bool,
) -> FollowUpDecision:
    """The core rule: mention + match + APPLIED + actionable => follow-up.

    - role_matched=False (different role, no tracked row) => never follow up.
    - NOT_APPLIED / NOT_INTERESTED => never a post-application follow-up.
    - UNKNOWN / NOT_SURE / ELIGIBLE_NOT_APPLIED + actionable application
      message => ask whether they applied (nudge), do NOT follow up.
    - APPLIED + post-application action (or actionable application update)
      => follow up; everything else is informational.
    """
    if not role_matched:
        return FollowUpDecision(
            should_follow_up=False, suggest_ask_applied=False,
            reason="message role does not match a tracked application — "
                   "never assume one role's application covers another",
            priority=NotificationPriority.LOW)
    if status in _ANSWERED_NEGATIVE:
        return FollowUpDecision(
            should_follow_up=False, suggest_ask_applied=False,
            reason=f"application status is {status.value} — company mention "
                   "alone is not proof of application; post-application "
                   "follow-up suppressed",
            priority=NotificationPriority.LOW)
    if status in _UNDECIDED:
        actionable = (
            analysis.message_type
            is ApplicationMessageType.POST_APPLICATION_ACTION
            or (analysis.message_type
                is ApplicationMessageType.APPLICATION_RELATED
                and analysis.requires_action))
        if actionable:
            return FollowUpDecision(
                should_follow_up=False, suggest_ask_applied=True,
                reason=f"application status is {status.value} — ask whether "
                       "the student applied before treating this as a "
                       "post-application follow-up",
                priority=analysis.priority)
        return FollowUpDecision(
            should_follow_up=False, suggest_ask_applied=False,
            reason="pre-application or informational message with undecided "
                   "application status — no follow-up",
            priority=NotificationPriority.LOW)
    # status is APPLIED from here on.
    if analysis.message_type is ApplicationMessageType.POST_APPLICATION_ACTION:
        return FollowUpDecision(
            should_follow_up=True, suggest_ask_applied=False,
            reason="applied + post-application action required"
                   + (" (consequence/penalty mentioned — high priority)"
                      if analysis.has_consequence else ""),
            priority=analysis.priority)
    if (analysis.message_type is ApplicationMessageType.APPLICATION_RELATED
            and analysis.requires_action):
        return FollowUpDecision(
            should_follow_up=True, suggest_ask_applied=False,
            reason="applied + actionable application update",
            priority=NotificationPriority.MEDIUM)
    return FollowUpDecision(
        should_follow_up=False, suggest_ask_applied=False,
        reason="applied but the message is a pre-application opening or "
               "generic noise — not a post-application follow-up",
        priority=NotificationPriority.LOW)
