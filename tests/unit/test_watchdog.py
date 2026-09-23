"""Failed-job watchdog: only NEW failures are recorded, internals never reach
the student channel (no func names, traces, or queue depth) — full detail
goes to the optional admin channel, plain heartbeat note to the student.
RQ/Redis are hand-rolled fakes — no live services."""

import json
from types import SimpleNamespace

import pytest

from pia_worker.jobs import watchdog as wd


class FakeJob:
    def __init__(self, job_id, func, exc="Traceback\nValueError: boom"):
        self.id = job_id
        self.func_name = func
        self.exc_info = exc


class FakeRegistry:
    def __init__(self, ids):
        self._ids = ids

    def get_job_ids(self):
        return list(self._ids)


class FakeQueue:
    depths: dict[str, int] = {}

    def __init__(self, name):
        self.name = name

    @property
    def count(self) -> int:
        return FakeQueue.depths.get(self.name, 0)


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.fail_get = False

    def get(self, key):
        if self.fail_get:
            raise ConnectionError("redis down")
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value if isinstance(value, str) else value.decode()


JOBS = [FakeJob("a", "pia_worker.jobs.process_message.download_attachment"),
        FakeJob("b", "pia_worker.jobs.notify.notify_event")]


def _settings(admin: str = "", debug: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        watch_queue_depth_threshold=300, watch_heartbeat_stale_seconds=300,
        admin_telegram_chat_id=admin, debug_notifications_enabled=debug)


def _wire(monkeypatch: pytest.MonkeyPatch, jobs, seen=None,
          fail_get: bool = False, admin: str = "",
          debug: bool = False) -> tuple[FakeRedis, list]:
    import datetime as dt

    redis = FakeRedis()
    if seen is not None:
        redis.store[wd._STATE_KEY] = json.dumps(seen)
    redis.store["pia:worker:heartbeat"] = dt.datetime.now(
        dt.UTC).isoformat()
    redis.fail_get = fail_get
    FakeQueue.depths = {}
    by_id = {j.id: j for j in jobs}
    monkeypatch.setattr(wd, "make_connection", lambda *a, **k: redis)
    monkeypatch.setattr(wd, "Queue", lambda name, connection=None: FakeQueue(name))
    monkeypatch.setattr(
        wd, "FailedJobRegistry",
        lambda queue=None: FakeRegistry(list(by_id) if queue.name == "default" else []))
    monkeypatch.setattr(wd.Job, "fetch",
                        staticmethod(lambda i, connection=None: by_id[i]))
    monkeypatch.setattr(wd, "get_settings", lambda: _settings(admin, debug))
    sent: list = []
    monkeypatch.setattr(wd, "_telegram_send",
                        lambda **kw: sent.append(kw) or True)
    return redis, sent


def _student_texts(sent: list) -> list[str]:
    return [s["text"] for s in sent if "chat_id" not in s]


def _admin_texts(sent: list) -> list[str]:
    return [s["text"] for s in sent if "chat_id" in s]


class TestWatchdog:
    def test_first_scan_counts_without_student_spam(
            self, monkeypatch) -> None:
        _, sent = _wire(monkeypatch, JOBS)
        result = wd.scan_failed_jobs()
        assert result == {"failed": 2, "new": 2, "max_depth": 0,
                          "heartbeat_stale": False}
        assert sent == []  # no admin channel: internals stay in logs

    def test_second_scan_is_quiet(self, monkeypatch) -> None:
        redis, sent = _wire(monkeypatch, JOBS)
        wd.scan_failed_jobs()
        sent.clear()
        result = wd.scan_failed_jobs()
        assert result == {"failed": 2, "new": 0, "max_depth": 0,
                          "heartbeat_stale": False}
        assert sent == []

    def test_only_delta_counts(self, monkeypatch) -> None:
        _, sent = _wire(monkeypatch, JOBS, seen=["a", "b"])
        result = wd.scan_failed_jobs()
        assert result == {"failed": 2, "new": 0, "max_depth": 0,
                          "heartbeat_stale": False}
        assert sent == []

    def test_redis_blip_sends_only_plain_heartbeat_note(
            self, monkeypatch) -> None:
        _, sent = _wire(monkeypatch, JOBS, fail_get=True)
        assert wd.scan_failed_jobs()["new"] == 2
        assert len(sent) == 1  # heartbeat unreadable -> stale note only
        assert "may be delayed" in sent[0]["text"]
        assert "Traceback" not in sent[0]["text"]
        assert "download_attachment" not in sent[0]["text"]

    def test_state_bounded(self, monkeypatch) -> None:
        redis, _ = _wire(monkeypatch, JOBS)
        monkeypatch.setattr(wd, "_MAX_TRACKED", 1)
        wd.scan_failed_jobs()
        assert len(json.loads(redis.store[wd._STATE_KEY])) == 1

    def test_deep_queue_never_reaches_student(
            self, monkeypatch) -> None:
        _, sent = _wire(monkeypatch, [], seen=[])
        FakeQueue.depths = {"default": 450}
        result = wd.scan_failed_jobs()
        assert result["max_depth"] == 450
        assert sent == []

    def test_stale_heartbeat_plain_note_no_traces(
            self, monkeypatch) -> None:
        import datetime as dt

        redis, sent = _wire(monkeypatch, JOBS, seen=[])
        redis.store["pia:worker:heartbeat"] = (
            dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat()
        result = wd.scan_failed_jobs()
        assert result["heartbeat_stale"] is True
        assert len(sent) == 1
        assert "may be delayed" in sent[0]["text"]
        assert "Traceback" not in sent[0]["text"]

    def test_admin_channel_gets_full_detail(
            self, monkeypatch) -> None:
        _, sent = _wire(monkeypatch, JOBS, admin="-999")
        wd.scan_failed_jobs()
        assert len(sent) == 1
        assert sent[0].get("chat_id") == "-999"
        assert "2 new failed" in sent[0]["text"]
        assert "download_attachment" in sent[0]["text"]

    def test_debug_flag_routes_detail_without_admin(
            self, monkeypatch) -> None:
        _, sent = _wire(monkeypatch, JOBS, debug=True)
        wd.scan_failed_jobs()
        assert len(sent) == 1
        assert "2 new failed" in sent[0]["text"]
