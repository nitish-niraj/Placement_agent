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
    "KYC": "Join via the link if you can — otherwise the listener attends " +
          "and sends you the summary + feedback form (DEC-008 amendment)",
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
    links: list[str] | None = None,
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
    for link in (links or [])[:2]:
        detail_lines += f"\n🔗 <b>Join/Link:</b> {html.escape(link)}"
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


def render_ask_applied(
    *, company: str | None, role: str | None, event_type: str,
    context_line: str | None = None,
) -> RenderedAlert:
    """Ask-whether-applied nudge: the student is eligible (or a post-application
    message arrived) but the application state is undecided. Sent with the
    ask_applied_keyboard — the answer persists the opportunity state."""
    role_line = f" for <b>{html.escape(role)}</b>" if role else ""
    alert = _e(
        NotificationPriority.MEDIUM,
        f"{company or 'General'} — Application check",
        f"You are eligible{role_line} and an application-related update "
        f"arrived ({event_type.replace('_', ' ').title()}).",
        "Future updates can only become follow-ups once the system knows "
        "whether you applied — a company mention alone is never proof.",
        "n/a",
        "Tap a button below: have you applied for this role?",
        context_line or "application-state check",
        None,
    )
    evidence = [{"kind": "application_check",
                 "ref": f"{company or ''}|{role or ''}"}]
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


# --- Student-first templates (context-first rendering) -----------------------
# Written for a student on a phone, not a developer: short, grouped, no IDs,
# no internal vocabulary. Empty fields are omitted, never printed blank.

_STAGE_EMOJI = {
    "Shortlist": "\U0001F3AF",  # 🎯 SHORTLISTED
    "Interview": "\U0001F3A4",  # 🎤 INTERVIEW
    "Online Test": "\U0001F4DD",  # 📝 ONLINE TEST
    "Assessment": "\U0001F9EA",  # 🧪 ASSESSMENT
    "Registration": "\U0001F4CB",  # 📋 REGISTRATION
    "Registration Check": "\U0001F4CB",
    "Document Submission": "\U0001F4C4",  # 📄 DOCUMENT SUBMISSION
    "Opportunity": "\U0001F7E0",  # 🟠 NEW OPPORTUNITY
    "Survey": "\U0001F4E3",  # 📢 (surveys rarely alert; kept distinct)
    "Training": "\U0001F4DA",
    "Notice": "\U0001F4E2",  # 📢 PLACEMENT UPDATE
    "Update": "\U0001F4E2",
    "System": "\u2699\ufe0f",
}

_ACTION_BY_RECOMMENDATION = {
    "ACTION_REQUIRED": "Act now — see the recommended action above",
    "REGISTER": "Register before the deadline if you want to participate",
    "ATTEND": "Attend as scheduled with the listed documents",
    "PREPARE": "Prepare and appear as per the shared schedule",
    "SUBMIT_DOCUMENTS": "Submit the listed documents before the deadline",
    "CHECK_RESULT": "Check whether your name appears and follow next steps",
    "VERIFY": "Confirm whether this applies to you before acting",
    "NO_ACTION_REQUIRED": "Nothing to do",
    "IGNORE_DUPLICATE": "Nothing to do — already covered",
}


def _deadline_line(ctx) -> str:  # noqa: ANN001 — ResolvedContext (circular-safe)
    """Deadline + relative urgency, or an honest unknown. Never invented."""
    from pia_worker.notify.context import DeadlineStatus

    if ctx.deadline is None:
        return "Not specified"
    local = ctx.deadline.astimezone(IST)
    base = local.strftime("%d %b %Y, %I:%M %p IST")
    now = datetime.now(tz=IST)
    if ctx.deadline_status is DeadlineStatus.EXPIRED:
        return f"Passed on {base}"
    delta = (local - now).total_seconds()
    if delta <= 0:
        return f"Passed on {base}"
    if local.date() == now.date():
        hours = max(int(delta // 3600), 1)
        return f"{base} (due today, ~{hours}h left)"
    days = int(delta // 86400)
    if days == 1:
        return f"{base} (due tomorrow)"
    if days < 7:
        return f"{base} (due in {days} days)"
    return base


def _why_received(ctx) -> str:  # noqa: ANN001
    """Specific, evidence-backed reason — never the generic ladder sentence."""
    from pia_worker.notify.context import AttachmentVerification

    verification = ctx.attachment_verification
    if verification is AttachmentVerification.USER_PRESENT:
        return ("Your record appears in the attached list"
                + (f" ({ctx.attachment_detail})"
                   if ctx.attachment_detail else "") + ".")
    if ctx.application_status == "APPLIED" and ctx.action_required:
        return (f"You applied for this {ctx.stage.lower()} and it needs "
                "your action.")
    if ctx.eligibility_status in ("ELIGIBLE", "USER_CONFIRMED"):
        return "You meet the eligibility criteria stated in the notice."
    if ctx.topic.value == "SHORTLIST":
        return "You were shortlisted for the next round."
    if ctx.topic.value == "INTERVIEW":
        return ("This is an interview for a company whose process "
                "you are part of.")
    if ctx.action_required:
        return "This announcement calls for action from candidates."
    return ("This message is informational and does not indicate that action "
            "is required from you.")


def _header(ctx, emoji: str, title: str) -> list[str]:  # noqa: ANN001
    lines = [f"{emoji} <b>{html.escape(title)}</b>", ""]
    subject = ctx.subject_display
    if ctx.company and subject != ctx.company:
        lines.append(f"🏢 {html.escape(ctx.company)} · {html.escape(subject)}")
    else:
        lines.append(f"🏢 {html.escape(subject)}")
    if ctx.role:
        lines.append(f"💼 {html.escape(ctx.role)}")
    if ctx.stage not in ("Update", "Opportunity"):
        lines.append(f"🎯 {html.escape(ctx.stage)}")
    return lines


def _footer(ctx, links: tuple[str, ...] = ()) -> list[str]:  # noqa: ANN001
    lines = [f"🔎 Why\n{_why_received(ctx)}"]
    shown = [str(link) for link in links if str(link).strip()][:2]
    for link in shown:
        lines.append(f"🔗 {html.escape(link)}")
    if ctx.source_group:
        lines.append(f"📌 Source\n{html.escape(ctx.source_group)}")
    return lines


def render_action_required(ctx, links: tuple[str, ...] = ()) -> RenderedAlert:  # noqa: ANN001
    """🔴 ACTION REQUIRED — the student must do something specific."""
    lines = _header(ctx, "🔴", "ACTION REQUIRED")
    lines += ["", "📌 What this is about",
              (ctx.source_message or ctx.stage)[:280]]
    lines += ["", "⏰ Deadline", _deadline_line(ctx)]
    if ctx.location:
        lines += ["", "📍 Location", html.escape(ctx.location)]
    lines += ["", "👉 Recommended action",
              html.escape(ctx.recommendation_text), ""]
    lines += _footer(ctx, links)
    evidence = [{"kind": ref["kind"], "ref": str(ref.get("ref") or "")}
                for ref in ctx.evidence_refs]
    return RenderedAlert("\n".join(lines), evidence)


def render_informational(ctx) -> RenderedAlert:  # noqa: ANN001
    """ℹ️ PLACEMENT UPDATE — nothing to do, but worth knowing."""
    lines = ["ℹ️ <b>PLACEMENT UPDATE</b>", "",
             f"🏢 {html.escape(ctx.subject_display)}", "",
             (ctx.source_message or ctx.stage)[:280], "", "📌 Status:",
             html.escape(ctx.recommendation_text), "", "🔎 Why",
             _why_received(ctx)]
    return RenderedAlert("\n".join(lines), [])


def render_opportunity(ctx, links: tuple[str, ...] = (),  # noqa: ANN001
                       package: str | None = None) -> RenderedAlert:
    """🟠 NEW OPPORTUNITY — role, compensation, deadline, recommendation."""
    lines = _header(ctx, "🟠", "NEW OPPORTUNITY")
    if package:
        lines += ["", "💰 Compensation:", html.escape(package)]
    if ctx.location:
        lines += ["", "📍 Location:", html.escape(ctx.location)]
    lines += ["", "⏰ Registration deadline:", _deadline_line(ctx), "",
              "👉 Recommendation:", html.escape(ctx.recommendation_text), ""]
    lines += _footer(ctx, links)
    return RenderedAlert("\n".join(lines), [])


def render_stage_update(ctx, links: tuple[str, ...] = ()) -> RenderedAlert:  # noqa: ANN001
    """Stage-categorized update (🎯 SHORTLISTED / 🎤 INTERVIEW / 📝 ONLINE
    TEST / …) — never collapsed into a generic REGISTRATION."""
    emoji = _STAGE_EMOJI.get(ctx.stage, "📢")
    title = ctx.stage.upper() if ctx.stage != "Update" else "PLACEMENT UPDATE"
    lines = _header(ctx, emoji, title)
    lines += ["", (ctx.source_message or ctx.stage)[:280], "",
              "⏰", _deadline_line(ctx), "", "👉",
              html.escape(ctx.recommendation_text), ""]
    lines += _footer(ctx, links)
    return RenderedAlert("\n".join(lines), [])


def render_expired(ctx) -> RenderedAlert:  # noqa: ANN001
    """⚫ EXPIRED — the deadline passed; informational only."""
    lines = _header(ctx, "⚫", "EXPIRED")
    lines += ["", (ctx.source_message or ctx.stage)[:280], "",
              "⏰ Deadline", _deadline_line(ctx), "", "👉 Recommendation:",
              html.escape(ctx.recommendation_text)]
    return RenderedAlert("\n".join(lines), [])


def render_verification_needed(ctx) -> RenderedAlert:  # noqa: ANN001
    """⚠️ VERIFICATION NEEDED — attachment could not prove anything."""
    lines = _header(ctx, "⚠️", "VERIFICATION NEEDED")
    lines += ["", (ctx.source_message or ctx.stage)[:280], "",
              "🔎 Attachment verification:",
              "Unable to confirm your status.", "",
              "👉 Recommendation:", html.escape(ctx.recommendation_text)]
    return RenderedAlert("\n".join(lines), [])


def render_no_action(ctx) -> RenderedAlert:  # noqa: ANN001
    """✅ NO ACTION REQUIRED — only sent when visibility is explicitly
    wanted; the default for absent/unneeded is silence."""
    lines = _header(ctx, "✅", "NO ACTION REQUIRED")
    lines += ["", (ctx.source_message or ctx.stage)[:280], "",
              "👉 Recommendation:", html.escape(ctx.recommendation_text)]
    return RenderedAlert("\n".join(lines), [])


def render_event(ctx, links: tuple[str, ...] = (),  # noqa: ANN001
                 package: str | None = None) -> RenderedAlert:
    """Dispatcher: context -> the one student-facing template."""
    from pia_worker.notify.context import (
        AttachmentVerification,
        DeadlineStatus,
        Recommendation,
    )

    if ctx.deadline_status is DeadlineStatus.EXPIRED:
        return render_expired(ctx)
    if ctx.attachment_verification in (AttachmentVerification.NOT_AVAILABLE,
                                       AttachmentVerification.PARSE_FAILED):
        # Only when something must still be said (caller usually suppresses).
        return render_verification_needed(ctx)
    if ctx.recommendation is Recommendation.ACTION_REQUIRED:
        return render_action_required(ctx, links)
    if ctx.recommendation is Recommendation.NO_ACTION_REQUIRED:
        return render_informational(ctx)
    if ctx.recommendation is Recommendation.REGISTER:
        return render_opportunity(ctx, links, package)
    return render_stage_update(ctx, links)


def _describe_delta(delta: dict) -> list[str]:
    """Material change -> student-readable lines ("Deadline updated from …
    to …"). Unknown fields render generically, never dropped silently."""
    lines: list[str] = []
    for field_name, change in (delta or {}).items():
        if not isinstance(change, dict):
            continue
        before, after = change.get("from"), change.get("to")
        label = str(field_name).replace("_at", "").replace("_", " ").title()
        if field_name in ("deadline_at", "start_at") and before and after:
            try:
                before_s = datetime.fromisoformat(str(before)).strftime(
                    "%d %b · %I:%M %p")
                after_s = datetime.fromisoformat(str(after)).strftime(
                    "%d %b · %I:%M %p")
            except ValueError:
                before_s, after_s = str(before), str(after)
            lines.append(f"🔔 {label} updated\nfrom {before_s} to {after_s}")
        elif field_name == "links" and after:
            lines.append("🔔 New link added")
        else:
            lines.append(f"🔔 {label} updated"
                         + (f"\nnow: {after}" if after not in (None, "")
                            else ""))
    return lines


def render_event_updated(ctx, delta: dict,  # noqa: ANN001
                         links: tuple[str, ...] = ()) -> RenderedAlert:
    """🔔 UPDATED — what materially changed, not another "New announcement"."""
    lines = _header(ctx, "🔔", "UPDATED")
    changes = _describe_delta(delta)
    lines += [""] + (changes or [(ctx.source_message or ctx.stage)[:280]])
    lines += ["", "⏰", _deadline_line(ctx), "", "👉",
              html.escape(ctx.recommendation_text), ""]
    lines += _footer(ctx, links)
    evidence = [{"kind": ref["kind"], "ref": str(ref.get("ref") or "")}
                for ref in ctx.evidence_refs]
    return RenderedAlert("\n".join(lines), evidence)


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


# --- Categorized daily digest (one meaningful line per item) ------------------

_DIGEST_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("action", "🔴 ACTION REQUIRED"),
    ("deadlines", "🟠 UPCOMING DEADLINES"),
    ("interviews", "🎤 INTERVIEWS"),
    ("tests", "📝 TESTS / ASSESSMENTS"),
    ("registrations", "📋 REGISTRATIONS"),
    ("info", "✅ NO ACTION / INFORMATIONAL"),
)


def digest_category(item: dict) -> str:
    """Route one digest item (payload-rich, legacy-tolerant) to a category."""
    payload = item.get("payload") or {}
    priority = str(item.get("priority") or "")
    if priority in ("CRITICAL", "HIGH"):
        return "action"
    stage = str(payload.get("stage") or "")
    topic = str(payload.get("topic") or "")
    if stage == "Interview":
        return "interviews"
    if stage in ("Online Test", "Assessment"):
        return "tests"
    if stage in ("Registration", "Registration Check", "Opportunity") or \
            topic == "REGISTRATION":
        return "registrations"
    if payload.get("deadline"):
        return "deadlines"
    return "info"


def _digest_item_line(item: dict) -> str:
    """One scannable line: subject — stage — deadline — recommendation."""
    payload = item.get("payload") or {}
    subject = str(payload.get("subject") or item.get("company")
                  or "Placement Update")
    parts = [subject]
    stage = str(payload.get("stage") or "")
    if stage and stage != "Update":
        parts.append(stage)
    deadline = str(payload.get("deadline") or "")
    if deadline:
        try:
            due = datetime.fromisoformat(deadline)
            parts.append("⏰ " + due.strftime("%d %b · %I:%M %p"))
        except ValueError:
            parts.append("⏰ " + deadline[:16])
    rec = str(payload.get("recommendation_text")
              or payload.get("reason") or item.get("reason") or "").strip()
    if rec:
        parts.append("👉 " + rec[:90])
    return " — ".join(parts)[:220]


def render_digest_v2(date_label: str,
                     items: list[dict]) -> str | None:
    """Categorized digest: counts per category, meaningful lines only.
    Legacy payload-less rows fall back to their reason under INFO."""
    grouped: dict[str, list[str]] = {key: [] for key, _ in _DIGEST_CATEGORIES}
    for item in items:
        line = _digest_item_line(item)
        if line:
            grouped[digest_category(item)].append(line)
    total = sum(len(v) for v in grouped.values())
    if not total:
        return None
    out = ["\U0001F4CB <b>PIA DAILY PLACEMENT DIGEST</b>", date_label, ""]
    for key, title in _DIGEST_CATEGORIES:
        entries = grouped[key]
        if not entries:
            continue
        out.append(f"<b>{title}</b> — {len(entries)} item(s)")
        out.extend(f"{i}. {html.escape(line)}"
                   for i, line in enumerate(entries, 1))
        out.append("")
    return "\n".join(out).rstrip()
