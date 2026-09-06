"""F-024 §11.1 notification template rendering — pure functions.

Contract (master §11.1):
    Title: [priority emoji] [canonical event/company]
    What changed: one or two sentences
    Why it matters to you: explicit relevance reason
    Deadline/time: exact local date/time if known
    Action: what the user should do
    Source: group + message/document reference
    Confidence: optional, only when ambiguity exists

Output is Telegram HTML (parse_mode=HTML); every dynamic value is escaped.
"""

import html
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from pia_shared.enums import NotificationPriority

IST = ZoneInfo("Asia/Kolkata")

_EMOJI = {
    NotificationPriority.CRITICAL: "\U0001F534",  # red circle
    NotificationPriority.HIGH: "\U0001F7E0",      # orange circle
    NotificationPriority.MEDIUM: "\U0001F7E1",    # yellow circle
    NotificationPriority.LOW: "\U0001F7E2",       # green circle
    NotificationPriority.IGNORE: "\u26AA",        # white circle
}

_ACTION_BY_TYPE = {
    "REGISTRATION": "Complete the registration before the deadline",
    "FORM": "Fill and submit the form before the deadline",
    "KYC": "Attend the KYC session — it cannot be done on your behalf (DEC-008)",
    "OA": "Prepare for and attend the online assessment",
    "EXAM": "Check the exam schedule and prepare accordingly",
    "INTERVIEW": "Report on time with the documents listed",
    "SHORTLIST": "Check the shortlist and follow the next-step instructions",
    "RESULT": "Check the announced result",
    "VENUE": "Note the updated venue for the event",
    "DOCUMENT_SUBMISSION": "Submit the listed documents on time",
    "JOINING": "Follow the joining/onboarding instructions",
    "OTHER": "Review the announcement and act if it applies to you",
}


@dataclass(frozen=True)
class RenderedAlert:
    text: str  # Telegram HTML, ready to send
    evidence_refs: list[dict]  # FR-NOT-007: typed evidence pointers


def _fmt_ist(value: datetime | None) -> str:
    if value is None:
        return "unknown (no date in the source — never invented, FR-EVT-005)"
    local = value.astimezone(IST)
    return local.strftime("%d %b %Y, %I:%M %p IST")


def _e(
    priority: NotificationPriority, title: str, what_changed: str, why_me: str,
    deadline_line: str, action: str, source_line: str,
    confidence_note: str | None,
) -> RenderedAlert:
    e = _EMOJI[priority]
    parts = [
        f"<b>{e} {html.escape(title)}</b>",
        f"<b>What changed:</b> {html.escape(what_changed)}",
        f"<b>Why it matters to you:</b> {html.escape(why_me)}",
        f"<b>Deadline/time:</b> {html.escape(deadline_line)}",
        f"<b>Action:</b> {html.escape(action)}",
        f"<b>Source:</b> {html.escape(source_line)}",
    ]
    if confidence_note:
        parts.append(f"<i>Confidence: {html.escape(confidence_note)}</i>")
    return RenderedAlert("\n".join(parts), evidence_refs=[])


def render_event_alert(
    *,
    priority: NotificationPriority,
    company: str | None,
    event_type: str,
    what_changed: str,
    why_me: str,
    deadline_at: datetime | None,
    start_at: datetime | None,
    venue: str | None,
    source_group: str | None,
    source_excerpt: str | None,
    source_message_id: str | None,
    event_id: str,
    designation: str | None = None,
    salary_package: str | None = None,
    job_location: str | None = None,
    eligibility_note: str | None = None,
) -> RenderedAlert:
    title = f"{company or 'General'} — {event_type.replace('_', ' ').upper()}"
    action = _ACTION_BY_TYPE.get(event_type, _ACTION_BY_TYPE["OTHER"])
    deadline_line = _fmt_ist(deadline_at)
    if start_at is not None:
        deadline_line = _fmt_ist(start_at)
    if venue:
        deadline_line += f" · Venue: {venue}"

    # Drive-detail lines — the role/package facts that decide whether an
    # opportunity is worth acting on (owner feedback, 2026-09-06).
    detail_lines = ""
    for emoji, label, value in (
        ("\U0001F4BC", "Role", designation),
        ("\U0001F4B0", "Package", salary_package),
        ("\U0001F4CD", "Location", job_location),
        ("\u2705", "Eligibility", eligibility_note),
    ):
        if value:
            detail_lines += (
                f"\n{emoji} <b>{html.escape(label)}:</b> {html.escape(str(value))}"
            )

    excerpt = (source_excerpt or "").strip().replace("\n", " ")[:140]
    source_line = f"{source_group or 'group'} · msg {source_message_id or event_id}"
    if excerpt:
        source_line += f" — \"{excerpt}\""

    alert = _e(priority, title, what_changed, why_me, deadline_line, action,
               source_line, None)
    if detail_lines:
        head, sep, tail = alert.text.partition("\n<b>Deadline/time:")
        if sep:
            alert = RenderedAlert(head + detail_lines + sep + tail, [])
    evidence = [{"kind": "message", "ref": source_message_id or event_id,
                 "quote": excerpt or None}]
    if source_group:
        evidence.append({"kind": "group", "ref": source_group})
    return RenderedAlert(alert.text, evidence)


def render_eligibility_alert(
    *, company: str, match_method: str | None, confidence: float | None,
    evidence_location: str | None, source_group: str | None,
) -> RenderedAlert:
    """Golden-scenario step 6: one eligibility alert with evidence (§20.2)."""
    why_me = (f"You are ELIGIBLE for {company} — matched via "
              f"{(match_method or 'IDENTIFIER').lower()} match")
    confidence_note = None
    if confidence is not None and confidence < 0.95:
        confidence_note = f"match confidence {confidence:.2f} — review recommended"
    alert = _e(
        NotificationPriority.HIGH,
        f"{company} — Eligibility",
        "Your name/registration number appears on the company's eligibility list",
        why_me,
        "n/a — eligibility has no deadline",
        "Track deadlines for this company; further updates will be prioritized",
        f"{source_group or 'group'} · list evidence at {evidence_location or 'sheet row'}",
        confidence_note,
    )
    evidence = [{"kind": "document", "ref": evidence_location or ""}]
    return RenderedAlert(alert.text, evidence)


def render_reminder(
    *, priority: NotificationPriority, company: str | None, event_type: str,
    due_at: datetime, window_hours: int,
) -> RenderedAlert:
    """FR-NOT-005 escalation reminder (T-24h / T-6h / T-1h)."""
    alert = _e(
        priority,
        f"{company or 'General'} — {event_type.replace('_', ' ').upper()}",
        f"Deadline escalation: {window_hours}h or less remaining",
        "Upcoming deadline for a tracked event (FR-NOT-005 escalation window)",
        _fmt_ist(due_at),
        _ACTION_BY_TYPE.get(event_type, _ACTION_BY_TYPE["OTHER"]),
        "deadline escalation (canonical event)",
        None,
    )
    return RenderedAlert(alert.text, [])


def render_digest(sections: list[tuple[str, list[str]]]) -> str | None:
    """F-025 digest: groups of (topic, item lines) — canonical events only.
    Returns None when there is nothing to send (empty-digest suppression)."""
    lines: list[str] = []
    for topic, items in sections:
        if not items:
            continue
        lines.append(f"<b>{html.escape(topic)}</b>")
        lines.extend(f"  • {html.escape(item)}" for item in items)
    if not lines:
        return None
    header = "\U0001F4CB <b>PIA daily digest</b>"
    return header + "\n" + "\n".join(lines)
