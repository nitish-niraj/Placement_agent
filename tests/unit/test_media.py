"""Media detection contract (FR-WA-006)."""

from pia_shared.media import guess_media


def test_document_with_filename_and_mime() -> None:
    item = {
        "message": {
            "documentMessage": {
                "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "fileName": "accenture_eligible.xlsx",
            }
        }
    }
    assert guess_media(item) == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "accenture_eligible.xlsx",
    )


def test_image_gets_derived_filename() -> None:
    mime, name = guess_media({"message": {"imageMessage": {"mimetype": "image/jpeg"}}})
    assert mime == "image/jpeg"
    assert name == "media.jpeg"


def test_document_with_caption_nested() -> None:
    item = {
        "message": {
            "documentWithCaptionMessage": {
                "message": {"documentMessage": {"mimetype": "application/pdf",
                                                "fileName": "list.pdf"}}
            }
        }
    }
    assert guess_media(item) == ("application/pdf", "list.pdf")


def test_text_message_has_no_media() -> None:
    assert guess_media({"message": {"conversation": "hello"}}) is None
    assert guess_media({}) is None
    assert guess_media({"message": {"imageMessage": {}}}) is None  # empty media object
