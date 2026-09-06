"""F-020 deterministic date/time parsing (FR-EVT-002, FR-EVT-005).

Pure functions, stdlib only. Every returned DateHit's `phrase` is an exact
substring of the source text — the FR-EVT-005 hallucination guard holds by
construction: nothing is ever resolved from a value that was not printed in
the message. Relative expressions resolve against `now` in Asia/Kolkata.

Unparseable text yields an empty list — a missing date stays unknown.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}

_MONTH = r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_ORD = r"(?:st|nd|rd|th)?"

# ISO: 2026-09-07 (explicit year — never rolled forward)
_ISO = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\b")
# Day-first numeric (Indian usage): 07/09/2026, 7-9-26, 07.09.2026
_NUMERIC = re.compile(r"\b(?P<d>\d{1,2})[/.-](?P<m>\d{1,2})[/.-](?P<y>\d{2,4})\b")
# "7 Sep 2026", "25th September" (year optional)
_DAY_MONTH = re.compile(r"\b(?P<d>\d{1,2})" + _ORD + r"\s*[- ]?" + _MONTH +
                        r"\.?(?:[, ]+\s*(?P<y>\d{4}))?\b", re.IGNORECASE)
# "Sep 7 2026", "September 25th" (year optional)
_MONTH_DAY = re.compile(r"\b" + _MONTH + r"\.?\s+(?P<d>\d{1,2})" + _ORD +
                        r"(?:[, ]+\s*(?P<y>\d{4}))?\b", re.IGNORECASE)

_WEEKDAY = re.compile(
    r"\b(?P<next>next\s+)?(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_RELATIVE = re.compile(
    r"\b(?P<rel>today|tonight|tomorrow|day after tomorrow)\b|"
    r"\bwithin\s+(?P<n>\d+)\s+(?P<unit>hours?|days?)\b|"
    r"\bin\s+(?P<n2>\d+)\s+(?P<unit2>days?)\b",
    re.IGNORECASE,
)
_TIME = re.compile(
    r"\b(?P<h>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>am|pm)\b|"
    r"\b(?P<h24>\d{1,2}):(?P<min24>\d{2})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DateHit:
    """One date phrase found in the source text (FR-EVT-002)."""

    phrase: str  # exact substring of the source text (FR-EVT-005 evidence)
    position: int  # offset of the phrase in the source text
    resolved: datetime  # tz-aware (Asia/Kolkata); 00:00 unless explicit time
    time_inferred: bool  # True when no explicit time appeared next to the date


def deadline_from_hit(hit: DateHit) -> datetime:
    """Deadline semantics for a parsed date: an explicit time stands; a
    date-only deadline means the END of that day (23:59 IST) — flagged via
    DateHit.time_inferred so consumers can phrase it as 'by <date>'."""
    if not hit.time_inferred:
        return hit.resolved
    return hit.resolved.replace(hour=23, minute=59)


def _resolve_day(day: int, month: int, year_text: str | None,
                 now: datetime) -> datetime | None:
    """Build a tz-aware date; roll to next year ONLY when the year was not
    printed and the date already passed ("7 Sep" said on 10 Sep 2026 means
    7 Sep 2027). Malformed dates return None — never invented (FR-EVT-005)."""
    try:
        if year_text is None:
            candidate = now.replace(month=month, day=day, hour=0, minute=0,
                                    second=0, microsecond=0)
            if candidate.date() < now.date():
                candidate = candidate.replace(year=candidate.year + 1)
            return candidate
        year = int(year_text)
        if year < 100:
            year += 2000
        return datetime(year, month, day, tzinfo=now.tzinfo)
    except ValueError:
        return None


def _apply_time(base: datetime, match: re.Match[str]) -> datetime:
    if match.group("h24") is not None:
        hour, minute = int(match.group("h24")), int(match.group("min24"))
    else:
        hour = int(match.group("h")) % 12
        if (match.group("ampm") or "").lower() == "pm":
            hour += 12
        minute = int(match.group("min") or 0)
    return base.replace(hour=hour, minute=minute)


def parse_date_mentions(text: str, now: datetime | None = None) -> list[DateHit]:
    """Parse date/time phrases from message text (FR-EVT-002).

    `now` anchors relative expressions; naive input is interpreted as IST and
    everything is resolved in Asia/Kolkata.
    """
    if not text:
        return []
    if now is None:
        now = datetime.now(tz=IST)
    now_local = (now if now.tzinfo else now.replace(tzinfo=IST)).astimezone(IST)

    hits: list[tuple[int, datetime, str, bool]] = []  # (pos, resolved, phrase, inferred)

    for match in _ISO.finditer(text):
        groups = match.groupdict()
        try:
            resolved = datetime(int(groups["y"]), int(groups["m"]), int(groups["d"]),
                                tzinfo=IST)
        except ValueError:
            continue
        hits.append((match.start(), resolved, match.group(0), True))

    for pattern in (_NUMERIC, _DAY_MONTH, _MONTH_DAY):
        for match in pattern.finditer(text):
            groups = match.groupdict()
            month = _MONTHS[groups["mon"][:3].lower()] if groups.get("mon") \
                else int(groups["m"])
            candidate = _resolve_day(int(groups["d"]), month, groups.get("y"),
                                     now_local)
            if candidate is not None:
                hits.append((match.start(), candidate, match.group(0), True))

    for match in _WEEKDAY.finditer(text):
        days_ahead = (_WEEKDAYS[match.group("wd").lower()] - now_local.weekday()) % 7
        if match.group("next"):
            days_ahead += 7
        resolved = now_local.replace(hour=0, minute=0, second=0, microsecond=0) \
            + timedelta(days=days_ahead)
        hits.append((match.start(), resolved, match.group(0), True))

    for match in _RELATIVE.finditer(text):
        rel = (match.group("rel") or "").lower()
        n = match.group("n") or match.group("n2")
        unit = match.group("unit") or match.group("unit2")
        base = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        time_inferred = True
        if rel in ("today", "tonight"):
            resolved = base
        elif rel == "tomorrow":
            resolved = base + timedelta(days=1)
        elif rel == "day after tomorrow":
            resolved = base + timedelta(days=2)
        elif unit and unit.lower().startswith("hour"):
            resolved = now_local + timedelta(hours=int(n))
            time_inferred = False  # "within 24 hours" IS the precise time
        else:
            resolved = base + timedelta(days=int(n or 0))
        hits.append((match.start(), resolved.replace(microsecond=0), match.group(0),
                     time_inferred))

    if not hits:
        return []

    # Attach explicit times to the nearest date hit (same announcement line).
    times = list(_TIME.finditer(text))
    dated: list[DateHit] = []
    for position, resolved, phrase, inferred in sorted(hits):
        time_inferred = inferred
        nearest = min(times, key=lambda m: abs(m.start() - position), default=None)
        if nearest is not None and abs(nearest.start() - position) <= 40:
            resolved = _apply_time(resolved, nearest)
            time_inferred = False
        dated.append(DateHit(phrase=phrase, position=position, resolved=resolved,
                             time_inferred=time_inferred))
    return dated
