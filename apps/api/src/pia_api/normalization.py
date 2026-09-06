"""Pure normalization helpers for Evolution API webhook payloads (FR-MSG-001).

Provider-shaped JSON in, canonical internal values out. Unit-tested without a DB.
"""

import datetime as dt
import hashlib
import re

from pia_shared.enums import InstanceStatus

_WS = re.compile(r"\s+")

_TEXT_SOURCES = (
    "conversation",
    "extendedTextMessage.text",
    "imageMessage.caption",
    "videoMessage.caption",
    "documentMessage.caption",
    "documentWithCaptionMessage.message.documentMessage.caption",
)


def extract_text(message: dict) -> str | None:
    """First available text body across common Evolution message shapes; None if none."""
    for path in _TEXT_SOURCES:
        value: object = message
        for part in path.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        if isinstance(value, str) and value.strip():
            return value
    return None


def parse_timestamp(raw: object) -> dt.datetime:
    """messageTimestamp arrives as seconds (str/int/float); epoch-ms detected by magnitude."""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return dt.datetime.now(dt.UTC)
    if value > 1e12:  # milliseconds
        value /= 1000.0
    return dt.datetime.fromtimestamp(value, tz=dt.UTC)


def collapse_text(text: str | None) -> str:
    if not text:
        return ""
    return _WS.sub(" ", text).strip()


def content_hash(text: str | None, message_type: str) -> str:
    """FR-DED-001 anchor: sha256 over collapsed text; media-only messages hash their type."""
    normalized = collapse_text(text) or f"{message_type}:no-text"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_group_jid(jid: str) -> bool:
    return jid.endswith("@g.us")


def map_connection_state(state: str | None) -> InstanceStatus:
    return {
        "open": InstanceStatus.CONNECTED,
        "connecting": InstanceStatus.RECONNECTING,
        "close": InstanceStatus.DISCONNECTED,
        "loggedOut": InstanceStatus.LOGGED_OUT,
        "qr": InstanceStatus.QR_PENDING,
    }.get(state or "", InstanceStatus.ERROR)


def date_only_expired(retention_days: int) -> dt.datetime:
    return dt.datetime.now(dt.UTC) + dt.timedelta(days=retention_days)
