"""Pure normalization helpers for Evolution API webhook payloads (FR-MSG-001).

Provider-shaped JSON in, canonical internal values out. Unit-tested without a DB.
"""

import datetime as dt
import hashlib
import re

from pia_shared.enums import InstanceStatus

_WS = re.compile(r"\s+")

# Text body sources across common Evolution message shapes (first hit wins).
# text_source provenance labels each hit (audit-only; evaluation reads
# messages.text unchanged). Replies keep the BODY source — quoted text is
# tracked separately via extract_reply, never merged.
_TEXT_SOURCE_LABELS = (
    ("conversation", "conversation"),
    ("extendedTextMessage.text", "extended_text"),
    ("imageMessage.caption", "image_caption"),
    ("videoMessage.caption", "video_caption"),
    ("documentMessage.caption", "document_caption"),
    ("documentWithCaptionMessage.message.documentMessage.caption",
     "document_caption"),
)

# Typed message objects that can carry a WhatsApp reply (contextInfo).
# Evolution mirrors WA shape: *.contextInfo{stanzaId, participant,
# quotedMessage}. quotedMessage re-uses normal shapes (conversation/text/caption).
_REPLY_CARRIERS = (
    "extendedTextMessage",
    "imageMessage",
    "videoMessage",
    "documentMessage",
)


def _walk(message: object, path: str) -> object:
    value: object = message
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def extract_text(message: dict) -> str | None:
    """First available text body across common Evolution message shapes; None if none."""
    text, _ = extract_text_with_source(message)
    return text


def extract_text_with_source(message: dict) -> tuple[str | None, str | None]:
    """(text, text_source). Source labels the winning body path; None source
    for media-only/empty messages. Reply contextInfo is ignored here — see
    extract_reply."""
    for path, label in _TEXT_SOURCE_LABELS:
        value = _walk(message, path)
        if isinstance(value, str) and value.strip():
            return value, label
    return None, None


def extract_reply(message: dict) -> dict | None:
    """Reply/quote edge from a WhatsApp message, or None when not a reply.

    Returns {stanza_id, participant, quoted_text}: stanza_id is the quoted
    message's provider id (joins messages.provider_message_id within the same
    group), participant its sender, quoted_text a ≤300-char audit snippet
    (reuses extract_text on the quoted payload — never classified separately).
    """
    for carrier in _REPLY_CARRIERS:
        node = _walk(message, carrier)
        if not isinstance(node, dict):
            continue
        context = node.get("contextInfo")
        if not isinstance(context, dict):
            continue
        stanza_id = context.get("stanzaId")
        if not isinstance(stanza_id, str) or not stanza_id.strip():
            continue
        participant = context.get("participant")
        quoted = context.get("quotedMessage")
        quoted_text = extract_text(quoted) if isinstance(quoted, dict) else None
        return {
            "stanza_id": stanza_id.strip(),
            "participant": (participant.strip() if isinstance(participant, str)
                            and participant.strip() else None),
            "quoted_text": (quoted_text[:300] if quoted_text else None),
        }
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
