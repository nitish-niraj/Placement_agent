"""Round 2 backpressure: over-budget groups get scheduled delays (never
drops), Redis outage fails open, delay params schedule instead of enqueue."""

import pytest

from pia_api import jobs as jobs_mod
from pia_api.routes import webhooks as wh
from pia_api.settings import Settings


class FakeRedis:
    def __init__(self, counts: dict | None = None, fail: bool = False):
        self.counts: dict[str, int] = dict(counts or {})
        self.fail = fail
        self.expires: dict[str, int] = {}

    def incr(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key: str, seconds: int) -> None:
        self.expires[key] = seconds


_last_redis: FakeRedis | None = None


class _RedisNamespace:
    @staticmethod
    def from_url(url: str, **kwargs) -> FakeRedis:
        assert _last_redis is not None
        return _last_redis


class FakeRedisModule:
    Redis = _RedisNamespace


class FakeSettings(Settings):
    webhook_burst_per_group: int = 3
    webhook_burst_window_seconds: int = 300


def _wire(monkeypatch: pytest.MonkeyPatch, redis: FakeRedis) -> None:
    global _last_redis
    _last_redis = redis
    monkeypatch.setattr(wh, "redis_lib", FakeRedisModule)
    monkeypatch.setattr(wh, "get_settings", lambda: FakeSettings(
        _env_file=None, dashboard_token="t"))  # type: ignore[call-arg]


class TestBurstDelay:
    def test_under_budget_no_delay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        redis = FakeRedis()
        _wire(monkeypatch, redis)
        assert wh._burst_delay_seconds("g1") == 0
        assert wh._burst_delay_seconds("g1") == 0
        assert wh._burst_delay_seconds("g1") == 0

    def test_over_budget_grows_and_caps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        redis = FakeRedis()
        _wire(monkeypatch, redis)
        for _ in range(3):
            assert wh._burst_delay_seconds("g1") == 0
        assert wh._burst_delay_seconds("g1") == 1
        assert wh._burst_delay_seconds("g1") == 2
        assert redis.expires.get("pia:burst:g1") == 300

    def test_no_group_no_redis_touch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        redis = FakeRedis()
        _wire(monkeypatch, redis)
        assert wh._burst_delay_seconds(None) == 0
        assert wh._burst_delay_seconds("") == 0
        assert redis.counts == {}

    def test_redis_outage_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, FakeRedis(fail=True))
        assert wh._burst_delay_seconds("g1") == 0


class FakeQueue:
    def __init__(self, name, connection=None):
        self.name = name
        self.calls: list = []

    def enqueue(self, *args, **kwargs):
        self.calls.append(("now", args))

    def enqueue_in(self, delay, *args, **kwargs):
        self.calls.append(("later", delay.total_seconds(), args))


class FakeJobsRedis:
    @staticmethod
    def from_url(url: str):
        return "redis-conn"


class TestDelayParams:
    def test_zero_delay_enqueues_now(self, monkeypatch: pytest.MonkeyPatch) -> None:
        queue = FakeQueue("default")
        monkeypatch.setattr(jobs_mod, "Queue", lambda name, connection=None: queue)
        monkeypatch.setattr(jobs_mod, "Redis", FakeJobsRedis)
        jobs_mod.enqueue_process_message("m", "c")
        assert queue.calls and queue.calls[0][0] == "now"

    def test_positive_delay_schedules(self, monkeypatch: pytest.MonkeyPatch) -> None:
        queue = FakeQueue("default")
        monkeypatch.setattr(jobs_mod, "Queue", lambda name, connection=None: queue)
        monkeypatch.setattr(jobs_mod, "Redis", FakeJobsRedis)
        jobs_mod.enqueue_process_message("m", "c", delay_seconds=45)
        jobs_mod.enqueue_download_attachment("a", delay_seconds=45)
        assert all(call[0] == "later" and call[1] == 45 for call in queue.calls)
        assert len(queue.calls) == 2
