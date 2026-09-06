"""Structured AI output schemas (TRD §4.2, master §12).

Every LLM task validates against one of these pydantic models before its output
can influence any state (ADR-004). Unknown/missing evidence must be None — the
null-when-unknown policy is enforced by making fields Optional and requiring the
model not to invent values (prompts repeat this; validators double-check).
"""

from pydantic import BaseModel, Field

from pia_shared.enums import Importance, MatchMethod, MatchStatus, MsgDomain


class Classification(BaseModel):
    """AI task 1 — message classifier (master §12)."""

    domain: MsgDomain
    importance: Importance
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class CompanyMention(BaseModel):
    name: str
    alias_of: str | None = None


class DateMention(BaseModel):
    raw: str  # the exact date phrase as it appears in the text
    resolved_at: str | None = None  # ISO-8601 with tz, only if resolvable from text
    relative_anchor: str | None = None  # e.g. "today", "tomorrow"


class ActionMention(BaseModel):
    type: str  # e.g. register, apply, attend, upload, pay
    description: str | None = None


class Entities(BaseModel):
    """AI task 2 — entity extractor (FR-CLS-003). Unknown fields stay null/empty."""

    companies: list[CompanyMention] = Field(default_factory=list)
    dates: list[DateMention] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    actions: list[ActionMention] = Field(default_factory=list)
    document_refs: list[str] = Field(default_factory=list)


class CandidateRow(BaseModel):
    """Normalized candidate row (master §8.1). Identity values never invented:
    absent identifiers stay None."""

    raw_name: str
    normalized_name: str
    roll_number: str | None = None
    registration_number: str | None = None
    student_id: str | None = None
    other_identifiers: dict[str, str] = Field(default_factory=dict)
    extraction_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_page_or_sheet: str = ""
    row_index: int = 0


class VisionExtraction(BaseModel):
    """AI task 4 — vision/OCR reviewer (master §12). Ambiguous names stay ambiguous."""

    rows: list[CandidateRow] = Field(default_factory=list)
    company: str | None = None
    is_candidate_list: bool = False
    notes: str | None = None


class EligibilityDetection(BaseModel):
    """AI task 3 — eligibility detector (FR-ELG-001). Must cite source cues."""

    is_candidate_list: bool
    company: str | None = None
    evidence: list[str] = Field(default_factory=list)


class EvidenceRef(BaseModel):
    """Typed evidence pointer (SEC-009: observed facts are citable, AI
    interpretation is labeled). `kind` is 'row' for candidate-list rows,
    'message'/'document' for source links; `location` is human-readable
    (e.g. 'Sheet1!row 266'), `quote` the observed source text."""

    kind: str
    location: str
    quote: str | None = None


class MatchResult(BaseModel):
    """Identity matching outcome (master §8.2 contract, TRD §6).

    One result per evaluated unit — a row from a candidate list. The
    Eligibility Engine aggregates row results into one eligibility_records
    state per list; no result ever auto-confirms an AMBIGUOUS match
    (ADR-005: the state machine has no AMBIGUOUS -> ELIGIBLE edge)."""

    status: MatchStatus
    match_method: MatchMethod | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    requires_user_review: bool = False
    matched_identity_id: str | None = None  # profile id when status is MATCHED
    matched_row_index: int | None = None  # row provenance inside the source list
    rationale: str = ""  # deterministic explanation — never LLM output


class AnswerCitation(BaseModel):
    """Source pointer inside a conversational answer (F-028: source-backed
    answers only). kind is 'message' | 'event' | 'eligibility'."""

    kind: str
    ref: str
    quote: str | None = None


class ConversationalAnswer(BaseModel):
    """P12 conversational search output. The model may ONLY restate facts from
    the retrieved context; validators and the API treat anything else as
    unavailable. Informational by definition (ADR-003): an answer never
    mutates eligibility/events state."""

    answer: str
    citations: list[AnswerCitation] = Field(default_factory=list)
    says_unavailable: bool = False
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class MeetingSummary(BaseModel):
    """P12+ Teams-listener output (Type-1 informational KYC, DEC-008 amendment).
    Composed ONLY from the live captions/chat transcript the listener captured;
    anything absent stays absent (FR-EVT-005 posture)."""

    company: str | None = None
    designation_discussed: str | None = None
    package_mentioned: str | None = None
    key_points: list[str] = Field(default_factory=list)
    form_link: str | None = None
    summary: str
