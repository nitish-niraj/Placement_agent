"""Normalization contract tests (FR-MSG-001)."""

import datetime as dt

from pia_api.normalization import (
    collapse_text,
    content_hash,
    extract_reply,
    extract_text,
    extract_text_with_source,
    is_group_jid,
    map_connection_state,
    parse_timestamp,
)
from pia_shared.enums import InstanceStatus


def test_extract_text_conversation() -> None:
    assert extract_text({"conversation": "hello world"}) == "hello world"


def test_extract_text_extended_and_caption() -> None:
    assert (
        extract_text({"extendedTextMessage": {"text": "reply"}}) == "reply"
    )
    assert (
        extract_text({"imageMessage": {"caption": "eligible list"}})
        == "eligible list"
    )


def test_extract_text_none_when_media_only() -> None:
    assert extract_text({"imageMessage": {"mimetype": "image/jpeg"}}) is None
    assert extract_text({}) is None


def test_extract_text_with_source_labels() -> None:
    assert extract_text_with_source({"conversation": "hi"}) == ("hi", "conversation")
    assert extract_text_with_source(
        {"extendedTextMessage": {"text": "reply"}}) == ("reply", "extended_text")
    assert extract_text_with_source(
        {"imageMessage": {"caption": "eligible list"}}) == (
            "eligible list", "image_caption")
    assert extract_text_with_source(
        {"videoMessage": {"caption": "v"}}) == ("v", "video_caption")
    assert extract_text_with_source(
        {"documentMessage": {"caption": "d"}}) == ("d", "document_caption")
    assert extract_text_with_source(
        {"imageMessage": {"mimetype": "image/jpeg"}}) == (None, None)


def test_extract_text_ignores_context_info() -> None:
    message = {"extendedTextMessage": {
        "text": "my answer",
        "contextInfo": {"stanzaId": "parent-1", "participant": "a@g.us",
                        "quotedMessage": {"conversation": "quoted body"}}}}
    assert extract_text(message) == "my answer"
    assert extract_text_with_source(message) == ("my answer", "extended_text")


def test_extract_reply_extended_text() -> None:
    message = {"extendedTextMessage": {
        "text": "noted",
        "contextInfo": {"stanzaId": "AAA111", "participant": "919@lid",
                        "quotedMessage": {"conversation": "original list"}}}}
    assert extract_reply(message) == {
        "stanza_id": "AAA111", "participant": "919@lid",
        "quoted_text": "original list"}


def test_extract_reply_media_carrier_and_caption_quote() -> None:
    message = {"imageMessage": {
        "caption": "my photo reply",
        "contextInfo": {"stanzaId": "BBB222", "participant": None,
                        "quotedMessage": {
                            "imageMessage": {"caption": "shortlist here"}}}}}
    assert extract_reply(message) == {
        "stanza_id": "BBB222", "participant": None,
        "quoted_text": "shortlist here"}


def test_extract_reply_none_when_not_a_reply() -> None:
    assert extract_reply({"conversation": "plain"}) is None
    assert extract_reply({"extendedTextMessage": {"text": "no ctx"}}) is None
    assert extract_reply({"extendedTextMessage": {
        "text": "x", "contextInfo": {"participant": "p"}}}) is None
    assert extract_reply({}) is None


def test_parse_timestamp_seconds_string() -> None:
    ts = parse_timestamp("1696848341")
    assert ts == dt.datetime.fromtimestamp(1696848341, tz=dt.UTC)


def test_parse_timestamp_milliseconds_detected() -> None:
    assert parse_timestamp(1696848341000) == dt.datetime.fromtimestamp(1696848341, tz=dt.UTC)


def test_parse_timestamp_invalid_falls_back_to_now() -> None:
    before = dt.datetime.now(dt.UTC)
    result = parse_timestamp(None)
    after = dt.datetime.now(dt.UTC)
    assert before <= result <= after


def test_content_hash_stable_across_whitespace() -> None:
    a = content_hash("Accenture  eligible\n list attached ", "conversation")
    b = content_hash("Accenture eligible list attached", "conversation")
    assert a == b
    assert content_hash(None, "imageMessage") == content_hash("", "imageMessage")


def test_collapse_text() -> None:
    assert collapse_text("  a\t\n b  ") == "a b"
    assert collapse_text(None) == ""


def test_group_jid_detection() -> None:
    assert is_group_jid("12036302@g.us")
    assert not is_group_jid("919999999999@s.whatsapp.net")


def test_connection_state_mapping() -> None:
    assert map_connection_state("open") is InstanceStatus.CONNECTED
    assert map_connection_state("close") is InstanceStatus.DISCONNECTED
    assert map_connection_state("qr") is InstanceStatus.QR_PENDING
    assert map_connection_state("weird") is InstanceStatus.ERROR
    assert map_connection_state(None) is InstanceStatus.ERROR
