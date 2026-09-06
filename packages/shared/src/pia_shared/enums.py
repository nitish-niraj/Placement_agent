"""Domain enums — mirror of docs/05_Backend_Schema.md §2.

These values are frozen (master plan convention 2: never renumber or rename).
Postgres enums in the initial Alembic migration use the exact same strings.
"""

from enum import StrEnum


class GroupCategory(StrEnum):
    PLACEMENT = "PLACEMENT"
    ACADEMIC = "ACADEMIC"
    ADMINISTRATIVE = "ADMINISTRATIVE"
    GENERAL = "GENERAL"
    OTHER = "OTHER"


class InstanceStatus(StrEnum):
    PENDING = "PENDING"
    QR_PENDING = "QR_PENDING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    DISCONNECTED = "DISCONNECTED"
    LOGGED_OUT = "LOGGED_OUT"
    ERROR = "ERROR"


class MessageState(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


class AttachmentState(StrEnum):
    PENDING = "PENDING"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    REJECTED_OVERSIZE = "REJECTED_OVERSIZE"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class MsgDomain(StrEnum):
    PLACEMENT = "PLACEMENT"
    ACADEMIC = "ACADEMIC"
    EXAMINATION = "EXAMINATION"
    ADMINISTRATIVE = "ADMINISTRATIVE"
    EVENT = "EVENT"
    GENERAL = "GENERAL"
    UNKNOWN = "UNKNOWN"


class Importance(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    IGNORE = "IGNORE"


class EligibilityState(StrEnum):
    UNKNOWN = "UNKNOWN"
    MATCHED = "MATCHED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    USER_CONFIRMED = "USER_CONFIRMED"
    ELIGIBLE = "ELIGIBLE"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class MatchStatus(StrEnum):
    MATCHED = "MATCHED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    INVALID_SOURCE = "INVALID_SOURCE"


class MatchMethod(StrEnum):
    EXACT = "EXACT"
    NORMALIZED = "NORMALIZED"
    IDENTIFIER = "IDENTIFIER"
    FUZZY = "FUZZY"
    MODEL_REVIEW = "MODEL_REVIEW"


class CompanyWatch(StrEnum):
    NONE = "NONE"
    WATCHING = "WATCHING"
    MUTED = "MUTED"


class EventType(StrEnum):
    REGISTRATION = "REGISTRATION"
    FORM = "FORM"
    KYC = "KYC"
    OA = "OA"
    EXAM = "EXAM"
    INTERVIEW = "INTERVIEW"
    SHORTLIST = "SHORTLIST"
    RESULT = "RESULT"
    VENUE = "VENUE"
    DOCUMENT_SUBMISSION = "DOCUMENT_SUBMISSION"
    JOINING = "JOINING"
    OTHER = "OTHER"


class EventStatus(StrEnum):
    DETECTED = "DETECTED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class DeadlineState(StrEnum):
    OPEN = "OPEN"
    DUE_SOON = "DUE_SOON"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class NotificationPriority(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    IGNORE = "IGNORE"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"
    PENDING_DELIVERY = "PENDING_DELIVERY"
    SUPPRESSED = "SUPPRESSED"


class MemoryScope(StrEnum):
    COMPANY = "COMPANY"
    EVENT = "EVENT"
    PROFILE = "PROFILE"
    GROUP = "GROUP"
    GENERAL = "GENERAL"


class ActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class JobState(StrEnum):
    QUEUED = "QUEUED"
    STARTED = "STARTED"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"


class CompanyLifecycle(StrEnum):
    """Application lifecycle stages (FR-MEM-004). Stored in
    companies.lifecycle_stage (text column — values frozen from the PRD).
    REGISTRATION onward are event-driven; they activate with P8 events."""

    DISCOVERED = "DISCOVERED"
    ELIGIBLE = "ELIGIBLE"
    REGISTRATION = "REGISTRATION"
    OA = "OA"
    SHORTLISTED = "SHORTLISTED"
    INTERVIEW = "INTERVIEW"
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"
