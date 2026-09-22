"""Inline-keyboard protocol: app:<uuid>:<STATUS> round-trips."""

from pia_shared.enums import ApplicationStatus
from pia_worker.notify.buttons import ask_applied_keyboard, parse_callback

APP_UUID = "12345678-1234-1234-1234-1234567890ab"


def test_keyboard_is_two_by_two_with_protocol_callbacks() -> None:
    kb = ask_applied_keyboard(APP_UUID)
    rows = kb["inline_keyboard"]
    assert len(rows) == 2 and all(len(r) == 2 for r in rows)
    datas = [b["callback_data"] for r in rows for b in r]
    assert len(set(datas)) == 4
    for data in datas:
        assert len(data) <= 64  # Telegram callback limit
        parsed = parse_callback(data)
        assert parsed is not None and parsed[0] == APP_UUID
    assert [p[1] for p in (parse_callback(d) for d in datas)] == [
        ApplicationStatus.APPLIED, ApplicationStatus.NOT_APPLIED,
        ApplicationStatus.NOT_SURE, ApplicationStatus.NOT_INTERESTED]


def test_parse_rejects_foreign_or_malformed() -> None:
    assert parse_callback(None) is None
    assert parse_callback("") is None
    assert parse_callback("bogus:data") is None
    assert parse_callback(f"app:{APP_UUID}:APPLIED:extra") is None
    assert parse_callback("app:not-a-uuid:APPLIED") is None
    assert parse_callback(f"other:{APP_UUID}:APPLIED") is None
