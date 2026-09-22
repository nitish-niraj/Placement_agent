"""Failed-job watchdog: only NEW failures alert, state survives restarts,
nothing is ever deleted. RQ/Redis are hand-rolled fakes — no live services."""

import json

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


def _wire(monkeypatch: pytest.MonkeyPatch, jobs, seen=None,
          fail_get: bool = False) -> tuple[FakeRedis, list]:
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
    sent: list = []
    monkeypatch.setattr(wd, "_telegram_send",
                        lambda **kw: sent.append(kw) or True)
    return redis, sent


class TestWatchdog:
    def test_first_scan_alerts_everything(self, monkeypatch) -> None:
        _, sent = _wire(monkeypatch, JOBS)
        result = wd.scan_failed_jobs()
        assert result == {"failed": 2, "new": 2, "max_depth": 0,
                          "heartbeat_stale": False}
        assert len(sent) == 1 and "2 new failed" in sent[0]["text"]

    def test_second_scan_is_quiet(self, monkeypatch) -> None:
        redis, sent = _wire(monkeypatch, JOBS)
        wd.scan_failed_jobs()
        assert sent  # first scan alerts
        sent.clear()
        result = wd.scan_failed_jobs()
        assert result == {"failed": 2, "new": 0, "max_depth": 0,
                          "heartbeat_stale": False}
        assert sent == []

    def test_only_delta_alerts(self, monkeypatch) -> None:
        _, sent = _wire(monkeypatch, JOBS, seen=["a", "b"])
        result = wd.scan_failed_jobs()
        assert result == {"failed": 2, "new": 0, "max_depth": 0,
                          "heartbeat_stale": False}
        assert sent == []

    def test_redis_blip_alerts_once(self, monkeypatch) -> None:
        _, sent = _wire(monkeypatch, JOBS, fail_get=True)
        assert wd.scan_failed_jobs()["new"] == 2
        assert len(sent) == 1

    def test_state_bounded(self, monkeypatch) -> None:
        redis, _ = _wire(monkeypatch, JOBS)
        monkeypatch.setattr(wd, "_MAX_TRACKED", 1)
        wd.scan_failed_jobs()
        assert len(json.loads(redis.store[wd._STATE_KEY])) == 1

    def test_deep_queue_alerts(self, monkeypatch) -> None:
        _, sent = _wire(monkeypatch, [], seen=[])
        FakeQueue.depths = {"default": 450}
        result = wd.scan_failed_jobs()
        assert result["max_depth"] == 450
        assert len(sent) == 1 and "queue depth high" in sent[0]["text"]
        assert "default" in sent[0]["text"]

    def test_stale_heartbeat_alerts(self, monkeypatch) -> None:
        import datetime as dt

        redis, sent = _wire(monkeypatch, [], seen=[])
        redis.store["pia:worker:heartbeat"] = (
            dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat()
        result = wd.scan_failed_jobs()
        assert result["heartbeat_stale"] is True
        assert len(sent) == 1 and "heartbeat stale" in sent[0]["text"]
