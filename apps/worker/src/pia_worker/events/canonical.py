"""F-019/F-021 pure event planning: canonical key (ADR-006 dedup unit),
EventPlan, and the material-field delta diff. No DB access here."""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pia_shared.enums import EventType
from pia_shared.textnorm import normalize_name


def canonical_key(
    company_key: str | None, event_type: EventType, action: str | None,
    deadline_at: datetime | None, link: str | None,
) -> str:
    """TRD §7: hash of the fact tuple (company, type, action, deadline, link).
    Order-insensitive components are normalized; identical facts MUST hash
    identically regardless of whitespace/case noise."""
    raw = "|".join([
        normalize_name(company_key) if company_key else "",
        event_type.value,
        normalize_name(action) if action else "",
        deadline_at.isoformat() if deadline_at else "",
        (link or "").rstrip("/").lower(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class EventPlan:
    """Everything one message says about one event (built by the job)."""

    event_type: EventType
    company_id: str | None = None
    company_key: str | None = None
    title: str | None = None
    action: str | None = None
    deadline_at: datetime | None = None
    start_at: datetime | None = None
    links: list[str] = field(default_factory=list)
    venue: str | None = None
    date_phrase: str | None = None
    excerpt: str = ""  # observed source text (SEC-009 evidence)
    source_message_id: str | None = None  # FR-NOT-007 evidence chain
    designation: str | None = None  # role offered (drive template field)
    salary_package: str | None = None  # stipend/CTC as printed
    job_location: str | None = None
    eligibility_note: str | None = None

    def payload(self) -> dict:
        return {
            "action": self.action,
            "links": self.links,
            "venue": self.venue,
            "date_phrase": self.date_phrase,
            "excerpt": self.excerpt[:300],
            "source_message_id": self.source_message_id,
            "designation": self.designation,
            "salary_package": self.salary_package,
            "job_location": self.job_location,
            "eligibility_note": self.eligibility_note,
        }


def material_delta(
    old_deadline: datetime | None, old_start: datetime | None, old_payload: dict,
    plan: EventPlan,
) -> dict[str, dict[str, Any]]:
    """Pure F-021 diff: material fields only (deadline, start, links, venue).
    Empty dict = the re-announcement carries nothing new (suppressed)."""
    delta: dict[str, dict[str, Any]] = {}
    old_links = set(old_payload.get("links") or [])
    new_links = set(plan.links)
    if new_links - old_links:
        delta["links"] = {"from": sorted(old_links), "to": sorted(new_links)}
    if plan.deadline_at and old_deadline and plan.deadline_at != old_deadline:
        delta["deadline_at"] = {"from": old_deadline.isoformat(),
                                "to": plan.deadline_at.isoformat()}
    if plan.deadline_at and not old_deadline:
        delta["deadline_at"] = {"from": None, "to": plan.deadline_at.isoformat()}
    if plan.start_at and old_start and plan.start_at != old_start:
        delta["start_at"] = {"from": old_start.isoformat(),
                             "to": plan.start_at.isoformat()}
    if plan.venue and plan.venue != old_payload.get("venue"):
        delta["venue"] = {"from": old_payload.get("venue"), "to": plan.venue}
    for drive_field in ("designation", "salary_package", "job_location"):
        new_value = getattr(plan, drive_field)
        if new_value and new_value != old_payload.get(drive_field):
            delta[drive_field] = {"from": old_payload.get(drive_field),
                                  "to": new_value}
    return delta
