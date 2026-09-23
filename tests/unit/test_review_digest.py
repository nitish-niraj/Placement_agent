"""Weekly review digest: new-items-only, weekly anchor, 30d expiry."""

import contextlib
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pia_worker.jobs import notify as nt


class FakeResult:
    def __init__(self, value=None, mapping=None, mapping_list=None):
        self._value = value
        self._mapping = mapping
        self._list = mapping_list or []

    def scalar(self):
        return self._value

    def scalar_one(self):
        return self._value

    def mappings(self):
        return self

    def first(self):
        return self._mapping

    def all(self):
        return self._list


class FakeConn:
    def __init__(self, script):
        self._script = script
        self.executed: list = []

    def execute(self, sql, params=None):
        text = str(sql)
        self.executed.append((text, params))
        return self._script(text, params)


class FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return contextlib.nullcontext(self._conn)

    def begin(self):
        return contextlib.nullcontext(self._conn)


def _row(i: int):
    return {"id": f"att-{i}", "file_name": f"shot-{i}.jpeg",
            "created_at": datetime(2026, 9, 20, 10, 0,
                                   tzinfo=ZoneInfo("Asia/Kolkata")),
            "extractor": "rapidocr-v1", "group_name": "placements"}


def _script(items, fingerprint=None, claimed=True, expired=0):
    def script(sql: str, params: dict | None):
        if "FROM idempotency_keys" in sql:
            return FakeResult(
                mapping=SimpleNamespace(fingerprint=fingerprint))
        if "DISTINCT ON (a.id)" in sql:
            return FakeResult(mapping_list=items)
        if sql.strip().startswith("SELECT COUNT(*) FROM attachments"):
            return FakeResult(value=expired)
        if "INSERT INTO idempotency_keys" in sql:
            fp = json.loads(params["fp"])
            assert fp == [r["id"] for r in items
                          if r["id"] not in (set(json.loads(fingerprint or "[]")))], fp
            return FakeResult(
                mapping={"key": "k"} if claimed else None)
        return FakeResult()
    return script


def _run(monkeypatch, items, **kw):
    conn = FakeConn(_script(items, **kw))
    monkeypatch.setattr(nt, "_engine", lambda: FakeEngine(conn))
    monkeypatch.setattr(
        nt, "get_settings",
        lambda: SimpleNamespace(digest_enabled=True,
                                app_timezone="Asia/Kolkata",
                                telegram_bot_token="t", telegram_chat_id="c"))
    sent: list = []

    class Chan:
        def send(self, chat_id, text, reply_markup=None):
            sent.append(text)
            from pia_worker.notify.channel import DeliveryResult
            return DeliveryResult(True, "telegram")

    monkeypatch.setattr(nt, "_channel", lambda: Chan())
    return conn, sent


class TestReviewDigest:
    def test_sends_new_items(self, monkeypatch) -> None:
        _, sent = _run(monkeypatch, [_row(1), _row(2)])
        assert nt.review_digest() == "sent:2"
        assert len(sent) == 1
        assert "REVIEW NEEDED" in sent[0]
        assert "shot-1.jpeg" in sent[0]

    def test_quiet_when_nothing_new(self, monkeypatch) -> None:
        _, sent = _run(
            monkeypatch, [_row(1)],
            fingerprint=json.dumps(["att-1"]))
        assert nt.review_digest() == "nothing_new"
        assert sent == []

    def test_race_loser_skips(self, monkeypatch) -> None:
        _, sent = _run(monkeypatch, [_row(1)], claimed=False)
        assert nt.review_digest() == "duplicate"
        assert sent == []

    def test_failed_send_releases_anchor_for_retry(
            self, monkeypatch) -> None:
        conn, sent = _run(monkeypatch, [_row(1)], claimed=True)
        from pia_worker.notify.channel import DeliveryError

        class BadChan:
            def send(self, chat_id, text, reply_markup=None):
                raise DeliveryError("down")

        monkeypatch.setattr(nt, "_channel", lambda: BadChan())
        assert nt.review_digest() == "delivery_failed"
        assert sent == []
        deletes = [sql for sql, _ in conn.executed
                   if "DELETE FROM idempotency_keys" in sql]
        assert len(deletes) == 1

    def test_expired_counted_not_listed(self, monkeypatch) -> None:
        _, sent = _run(monkeypatch, [_row(1)], expired=41)
        assert nt.review_digest() == "sent:1"
        assert "aged out of review" in sent[0]
        assert sent[0].count("shot-") == 1

    def test_disabled_flag(self, monkeypatch) -> None:
        conn, sent = _run(monkeypatch, [_row(1)])
        import pia_worker.settings as _s  # noqa: F401 — documents flag owner
        monkeypatch.setattr(
            nt, "get_settings",
            lambda: SimpleNamespace(digest_enabled=False,
                                    app_timezone="Asia/Kolkata",
                                    telegram_bot_token="t",
                                    telegram_chat_id="c"))
        assert nt.review_digest() == "disabled"
        assert sent == []
        _ = conn
