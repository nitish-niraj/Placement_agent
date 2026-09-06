"""P10 notification decision core (F-023, FR-NOT-001..005) — pure functions.

Master §11 priority ladder (deterministic; code owns decisions, ADR-004):
1. KYC events                              -> CRITICAL (mandatory KYC)
2. deadline TODAY (Asia/Kolkata)           -> CRITICAL ("Deadline today")
3. start/reporting time within 24h         -> CRITICAL ("exam/reporting time")
4. eligible/watched company event          -> HIGH    (FR-NOT-002: any new
   high-value event for an eligible company)
5. OA/SHORTLIST/REGISTRATION/INTERVIEW
   with a deadline >24h away               -> HIGH    ("registration deadline >24h")
6. EXAM (academic, no urgency signal)      -> MEDIUM  (FR-NOT-003: academic items
   surface even without a company relation)
7. otherwise                               -> MEDIUM  (non-urgent update);
   VENUE-only informational events         -> LOW

Delivery mapping: CRITICAL/HIGH -> IMMEDIATE, MEDIUM/LOW -> DIGEST.
Dedup (FR-NOT-004): dedup_key = sha256(event_id | material state hash) — one
notification per canonical event state; a material delta changes the state
hash and therefore legitimately produces ONE update notification.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pia_shared.enums import EventType, NotificationPriority

IST = ZoneInfo("Asia/Kolkata")

IMMEDIATE_TYPES = (EventType.OA, EventType.SHORTLIST, EventType.REGISTRATION,
                   EventType.INTERVIEW)


@dataclass(frozen=True)
class EventContext:
    """What the decision needs about one canonical event."""

    event_id: str
    event_type: EventType
    deadline_at: datetime | None = None
    start_at: datetime | None = None
    has_company: bool = False
    company_eligible: bool = False  # eligibility ELIGIBLE/USER_CONFIRMED
    company_watching: bool = False  # watch_state == WATCHING


@dataclass(frozen=True)
class Decision:
    priority: NotificationPriority
    delivery: str  # 'immediate' | 'digest'
    reason: str  # explicit relevance rationale (FR-NOT-001)


def _ist(now: datetime) -> datetime:
    return (now if now.tzinfo else now.replace(tzinfo=IST)).astimezone(IST)


def priority_for_event(ctx: EventContext, now: datetime | None = None) -> Decision:
    now_ist = _ist(now or datetime.now(tz=IST))

    if ctx.event_type is EventType.KYC:
        return Decision(NotificationPriority.CRITICAL, "immediate",
                        "Company arrival session — your listener can join and brief you; " +
                        "post-selection sessions you always attend yourself (DEC-008)")
    if ctx.deadline_at is not None and ctx.deadline_at.astimezone(IST).date() \
            == now_ist.date():
        base = "Deadline is TODAY"
        if ctx.company_eligible or ctx.company_watching:
            return Decision(NotificationPriority.CRITICAL, "immediate",
                            f"{base} and you are eligible for this company — "
                            "action required")
        return Decision(NotificationPriority.CRITICAL, "immediate", base)
    if ctx.start_at is not None and timedelta(0) <= ctx.start_at - now_ist \
            <= timedelta(hours=24):
        return Decision(NotificationPriority.CRITICAL, "immediate",
                        "Reporting/exam time is within 24 hours")
    if ctx.company_eligible or ctx.company_watching:
        return Decision(NotificationPriority.HIGH, "immediate",
                        "New event for a company you are eligible for "
                        "(FR-NOT-002, watch auto-activated)")
    if ctx.event_type in IMMEDIATE_TYPES and ctx.deadline_at is not None \
            and ctx.deadline_at > now_ist:
        return Decision(NotificationPriority.HIGH, "immediate",
                        "Registration window announced (deadline more than 24h away)")
    if ctx.event_type is EventType.EXAM:
        return Decision(NotificationPriority.MEDIUM, "digest",
                        "Academic exam announcement — surfaced without company "
                        "relation (FR-NOT-003)")
    if ctx.event_type is EventType.VENUE:
        return Decision(NotificationPriority.LOW, "digest",
                        "Informational venue note")
    return Decision(NotificationPriority.MEDIUM, "digest",
                    "Non-urgent announcement")


def material_state_hash(
    deadline_at: datetime | None, start_at: datetime | None,
    venue: str | None, links: list[str] | None,
    designation: str | None = None, salary_package: str | None = None,
) -> str:
    """Hash of everything that makes an event state materially different — a
    delta changes the hash, which legitimately re-arms one notification. The
    offered role and package are material: a changed CTC is worth one alert."""
    raw = "|".join([
        deadline_at.isoformat() if deadline_at else "",
        start_at.isoformat() if start_at else "",
        (venue or "").strip().lower(),
        ",".join(sorted((link or "").rstrip("/") for link in (links or []))),
        (designation or "").strip().lower(),
        (salary_package or "").strip().lower(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def dedup_key(event_id: str, state_hash: str, kind: str = "event",
              window_hours: int | None = None) -> str:
    """FR-NOT-004 anchor: one notification per canonical event state.
    Escalation reminders key on the window so each fires exactly once per
    deadline value (a moved deadline re-arms the windows via the new state)."""
    suffix = f"reminder:{window_hours}" if window_hours is not None else kind
    raw = f"{event_id}|{state_hash}|{suffix}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def reminder_windows(priority: NotificationPriority,
                     policy_windows: tuple[int, ...] = (24, 6, 1)) -> tuple[int, ...]:
    """FR-NOT-005: CRITICAL escalates at every configured window; HIGH gets
    the T-24h reminder only; MEDIUM/LOW ride the digest (no reminders)."""
    if priority is NotificationPriority.CRITICAL:
        return policy_windows
    if priority is NotificationPriority.HIGH:
        return (max(policy_windows),) if policy_windows else (24,)
    return ()


def windows_crossed(
    due_at: datetime, now: datetime, windows: tuple[int, ...], already_sent: tuple,
) -> tuple[int, ...]:
    """Which escalation windows are inside their reminder horizon now and have
    not fired yet for this deadline value."""
    remaining = due_at - _ist(now)
    return tuple(
        window for window in sorted(windows, reverse=True)
        if window not in already_sent and timedelta(0) <= remaining
        <= timedelta(hours=window)
    )
