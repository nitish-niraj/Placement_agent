"""Context-first event understanding — the normalized object every Telegram
message is rendered from (never raw classification fields).

The pipeline builds ONE ResolvedContext per event notification: company/role,
stage topic, what action is required, whether the student is eligible/applied,
whether an attachment proved anything, deadline truth, and a recommendation
with its evidence chain. Renderers consume this; raw internals never reach
Telegram directly.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from pia_worker.notify.topics import MessageTopic

IST = ZoneInfo("Asia/Kolkata")


class DeadlineStatus(StrEnum):
    UPCOMING = "UPCOMING"
    TODAY = "TODAY"
    URGENT = "URGENT"  # under 24h remaining
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class Recommendation(StrEnum):
    ACTION_REQUIRED = "ACTION_REQUIRED"
    NO_ACTION_REQUIRED = "NO_ACTION_REQUIRED"
    VERIFY = "VERIFY"
    PREPARE = "PREPARE"
    REGISTER = "REGISTER"
    ATTEND = "ATTEND"
    SUBMIT_DOCUMENTS = "SUBMIT_DOCUMENTS"
    CHECK_RESULT = "CHECK_RESULT"
    IGNORE_DUPLICATE = "IGNORE_DUPLICATE"


class AttachmentVerification(StrEnum):
    """Strict states — never convert NOT_AVAILABLE into NOT_PRESENT, and
    never read "no parsed rows" as "user not present" (see resolve())."""
    USER_PRESENT = "USER_PRESENT"
    USER_ABSENT = "USER_ABSENT"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    PARSE_FAILED = "PARSE_FAILED"
    NOT_RELEVANT = "NOT_RELEVANT"


@dataclass(frozen=True)
class ResolvedContext:
    event_id: str
    company: str | None  # canonical company, or None for topic subjects
    subject_display: str  # never "General"
    role: str | None
    event_type: str
    topic: MessageTopic
    stage: str  # student-facing stage label (Interview, Registration, …)
    action_required: bool
    eligibility_status: str  # ELIGIBLE / NOT_FOUND / UNKNOWN / NOT_APPLICABLE
    application_status: str  # APPLIED / NOT_APPLIED / … / UNKNOWN
    deadline: datetime | None
    deadline_status: DeadlineStatus
    location: str | None
    source_message: str  # short excerpt, never the whole raw dump
    source_message_id: str | None
    source_group: str | None
    attachments: tuple[str, ...]  # file names
    attachment_verification: AttachmentVerification
    attachment_detail: str  # e.g. "matched registration_number 12515641"
    confidence: float  # 0..1 subject confidence
    recommendation: Recommendation
    recommendation_text: str  # one plain-English sentence for the student
    reason: str  # why-me evidence sentence
    evidence_refs: tuple[dict, ...] = field(default_factory=tuple)
    dedupe_key: str = ""


def deadline_status(due_at: datetime | None,
                    now: datetime | None = None) -> DeadlineStatus:
    """Explicit deadline truth. A passed deadline is EXPIRED even when it is
    still "today" on the calendar — never render those as TODAY."""
    if due_at is None:
        return DeadlineStatus.UNKNOWN
    now_ist = (now or datetime.now(tz=IST))
    now_ist = (now_ist if now_ist.tzinfo else now_ist.replace(tzinfo=IST)
               ).astimezone(IST)
    due = (due_at if due_at.tzinfo else due_at.replace(tzinfo=IST)
           ).astimezone(IST)
    if due <= now_ist:
        return DeadlineStatus.EXPIRED
    if due.date() == now_ist.date():
        return DeadlineStatus.TODAY
    if (due - now_ist).total_seconds() <= 24 * 3600:
        return DeadlineStatus.URGENT
    return DeadlineStatus.UPCOMING


_STAGE_BY_TOPIC = {
    MessageTopic.SHORTLIST: "Shortlist",
    MessageTopic.INTERVIEW: "Interview",
    MessageTopic.ONLINE_TEST: "Online Test",
    MessageTopic.ASSESSMENT: "Assessment",
    MessageTopic.DOCUMENT_SUBMISSION: "Document Submission",
    MessageTopic.REGISTRATION: "Registration",
    MessageTopic.DEFAULTER_CHECK: "Registration Check",
    MessageTopic.SURVEY: "Survey",
    MessageTopic.TRAINING_SESSION: "Training",
    MessageTopic.PLACEMENT_OPPORTUNITY: "Opportunity",
    MessageTopic.ADMINISTRATIVE: "Notice",
    MessageTopic.INFORMATIONAL: "Update",
    MessageTopic.SYSTEM_TEST: "System",
    MessageTopic.UNKNOWN: "Update",
}


def recommend(*, topic: MessageTopic, verification: AttachmentVerification,
              applied: bool, action_required: bool,
              deadline_state: DeadlineStatus) -> tuple[Recommendation, str]:
    """Recommendation + one plain-English sentence. Factual, never
    motivational filler; uncertainty is stated, never hidden."""
    if verification is AttachmentVerification.USER_ABSENT:
        return (Recommendation.NO_ACTION_REQUIRED,
                "Your record was not found in the attached list — "
                "no action is required based on this message.")
    if verification in (AttachmentVerification.NOT_AVAILABLE,
                        AttachmentVerification.PARSE_FAILED):
        return (Recommendation.VERIFY,
                "The attached file could not be verified — please check the "
                "original placement message manually.")
    if deadline_state is DeadlineStatus.EXPIRED:
        return (Recommendation.NO_ACTION_REQUIRED,
                "The deadline for this has already passed.")
    if (topic is MessageTopic.DEFAULTER_CHECK
            and not applied
            and verification is AttachmentVerification.USER_PRESENT):
        return (Recommendation.ACTION_REQUIRED,
                "Your record appears in the defaulter list — complete the "
                "pending registration.")
    if topic is MessageTopic.REGISTRATION:
        return (Recommendation.REGISTER,
                "Register before the deadline if you want to participate.")
    if topic is MessageTopic.INTERVIEW:
        return (Recommendation.ATTEND,
                "Attend as scheduled with the listed documents.")
    if topic in (MessageTopic.ONLINE_TEST, MessageTopic.ASSESSMENT):
        return (Recommendation.PREPARE,
                "Prepare and appear as per the shared schedule.")
    if topic is MessageTopic.DOCUMENT_SUBMISSION:
        return (Recommendation.SUBMIT_DOCUMENTS,
                "Submit the listed documents before the deadline.")
    if topic is MessageTopic.SHORTLIST:
        return (Recommendation.CHECK_RESULT,
                "Check whether your name appears and follow the next steps.")
    if action_required and applied:
        return (Recommendation.ACTION_REQUIRED,
                "This needs your action as an applicant.")
    if action_required:
        return (Recommendation.VERIFY,
                "Confirm whether this applies to you before acting.")
    return (Recommendation.NO_ACTION_REQUIRED,
            "Informational — no action is required based on this message.")


def build_context(
    *, event_id: str, company: str | None, subject_display: str,
    role: str | None, event_type: str, topic: MessageTopic,
    action_required: bool, eligibility_status: str,
    application_status: str, deadline: datetime | None,
    location: str | None, source_message: str,
    source_message_id: str | None, source_group: str | None,
    attachments: tuple[str, ...] = (),
    verification: AttachmentVerification = AttachmentVerification.NOT_RELEVANT,
    attachment_detail: str = "", confidence: float = 0.0,
    reason: str = "", now: datetime | None = None,
    dedupe_key: str = "",
) -> ResolvedContext:
    """Assemble the normalized object renderers consume."""
    state = deadline_status(deadline, now)
    rec, rec_text = recommend(
        topic=topic, verification=verification,
        applied=application_status == "APPLIED",
        action_required=action_required, deadline_state=state)
    refs: list[dict] = [
        {"kind": "message", "ref": source_message_id or event_id,
         "quote": (source_message or "")[:140] or None},
    ]
    if event_id:
        refs.append({"kind": "event", "ref": event_id})
    if attachments:
        refs.append({"kind": "attachments", "ref": ", ".join(attachments)})
    if verification is not AttachmentVerification.NOT_RELEVANT:
        refs.append({"kind": "attachment_verification",
                     "ref": verification.value,
                     "result": attachment_detail or None})
    return ResolvedContext(
        event_id=event_id, company=company, subject_display=subject_display,
        role=role, event_type=event_type, topic=topic,
        stage=_STAGE_BY_TOPIC.get(topic, "Update"),
        action_required=action_required,
        eligibility_status=eligibility_status,
        application_status=application_status, deadline=deadline,
        deadline_status=state, location=location,
        source_message=(source_message or "")[:300],
        source_message_id=source_message_id, source_group=source_group,
        attachments=attachments, attachment_verification=verification,
        attachment_detail=attachment_detail, confidence=confidence,
        recommendation=rec, recommendation_text=rec_text, reason=reason,
        evidence_refs=tuple(refs), dedupe_key=dedupe_key)
