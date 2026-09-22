"""Telegram inline keyboards for application-state answers.

Callback protocol: `app:<application_states.id>:<STATUS>` where STATUS is one
of APPLIED / NOT_APPLIED / NOT_SURE / NOT_INTERESTED. The row id (uuid, 36
chars) keeps callbacks well under Telegram's 64-byte limit and needs no
company resolution at answer time — the row already identifies the exact
company+role opportunity.
"""

import re

from pia_shared.enums import ApplicationStatus

_CALLBACK_RE = re.compile(
    r"^app:([0-9a-fA-F-]{36}):(APPLIED|NOT_APPLIED|NOT_SURE|NOT_INTERESTED)$")

_BUTTONS: tuple[tuple[str, ApplicationStatus], ...] = (
    ("✅ Applied", ApplicationStatus.APPLIED),
    ("❌ Not Applied", ApplicationStatus.NOT_APPLIED),
    ("🤔 Not Sure", ApplicationStatus.NOT_SURE),
    ("🚫 Not Interested", ApplicationStatus.NOT_INTERESTED),
)


def ask_applied_keyboard(application_id: str) -> dict:
    """2x2 answer keyboard for the ask-whether-applied nudge."""
    rows = []
    pair: list[dict] = []
    for label, status in _BUTTONS:
        pair.append({"text": label,
                     "callback_data": f"app:{application_id}:{status.value}"})
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:  # pragma: no cover — even button count today
        rows.append(pair)
    return {"inline_keyboard": rows}


def parse_callback(data: str | None) -> tuple[str, ApplicationStatus] | None:
    """Callback data -> (application_states.id, status), or None when foreign."""
    if not data:
        return None
    match = _CALLBACK_RE.match(data.strip())
    if match is None:
        return None
    return match.group(1), ApplicationStatus(match.group(2))
