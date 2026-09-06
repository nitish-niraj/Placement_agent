"""Media detection for Evolution message payloads (FR-WA-006).

Pure helpers shared by the API (attachment registration at ingestion) and the
worker (download pipeline). Unit-tested without a DB.
"""

MEDIA_KEYS = (
    "imageMessage",
    "videoMessage",
    "documentMessage",
    "audioMessage",
    "stickerMessage",
    "pttMessage",
    "documentWithCaptionMessage",
)


def guess_media(item: dict) -> tuple[str, str | None] | None:
    """(mime_type, file_name) for media messages, else None."""
    message = item.get("message") or {}
    document_with_caption = (message.get("documentWithCaptionMessage") or {}).get(
        "message"
    ) or {}
    for key in MEDIA_KEYS:
        media = message.get(key)
        if key == "documentWithCaptionMessage":
            media = document_with_caption.get("documentMessage")
        if not isinstance(media, dict) or not media:
            continue
        mime = media.get("mimetype") or "application/octet-stream"
        file_name = media.get("fileName")
        if not file_name:
            ext = mime.split("/")[-1].split(";")[0] or "bin"
            file_name = f"media.{ext}"
        return mime, file_name
    return None
