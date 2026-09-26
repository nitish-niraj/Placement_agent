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

    def set(self, key, value, nx=False, ex=None):
        if isinstance(value, bytes):
            value = value.decode()
        elif not isinstance(value, str):
            value = str(value)
        if nx and key in self.store:
            return None
        self.store[key] = value
        _ = ex
        return True


JOBS = [FakeJob("a", "pia_worker.jobs.process_message.download_attachment"),
        FakeJob("b", "pia_worker.jobs.notify.notify_event")]


def _settings(admin: str = "", debug: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        watch_queue_depth_threshold=300, watch_heartbeat_stale_seconds=300,
        admin_telegram_chat_id=admin, debug_notifications_enabled=debug,
        evolution_base_url="", evolution_api_key="",
        evolution_instance_name="pia")


def _wire(monkeypatch: pytest.MonkeyPatch, jobs, seen=None,
          fail_get: bool = False, admin: str = "",
          debug: bool = False) -> tuple[FakeRedis, list]:
    import datetime as dt

    redis = FakeRedis()
    if seen is not None:
        redis.store[wd._STATE_KEY] = json.dumps(seen)
    redis.store["pia:worker:heartbeat"] = dt.datetime.now(
        dt.UTC).isoformat()
    redis.store[wd._ASK_HEARTBEAT_KEY] = dt.datetime.now(
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
                          "heartbeat_stale": False, "ask_stale": False, "evolution": "unknown"}
        assert sent == []  # no admin channel: internals stay in logs

    def test_second_scan_is_quiet(self, monkeypatch) -> None:
        redis, sent = _wire(monkeypatch, JOBS)
        wd.scan_failed_jobs()
        sent.clear()
        result = wd.scan_failed_jobs()
        assert result == {"failed": 2, "new": 0, "max_depth": 0,
                          "heartbeat_stale": False, "ask_stale": False, "evolution": "unknown"}
        assert sent == []

    def test_only_delta_counts(self, monkeypatch) -> None:
        _, sent = _wire(monkeypatch, JOBS, seen=["a", "b"])
        result = wd.scan_failed_jobs()
        assert result == {"failed": 2, "new": 0, "max_depth": 0,
                          "heartbeat_stale": False, "ask_stale": False, "evolution": "unknown"}
        assert sent == []

    def test_redis_blip_sends_only_plain_heartbeat_note(
            self, monkeypatch) -> None:
        _, sent = _wire(monkeypatch, JOBS, fail_get=True)
        assert wd.scan_failed_jobs()["new"] == 2
        assert len(sent) == 2  # worker + ask notes, both plain language
        assert "may be delayed" in sent[0]["text"]
        assert "slow to answer" in sent[1]["text"]
        for piece in sent:
            assert "Traceback" not in piece["text"]
            assert "download_attachment" not in piece["text"]

    def test_stale_ask_heartbeat_notes_only_ask(
            self, monkeypatch) -> None:
        import datetime as dt

        redis, sent = _wire(monkeypatch, [], seen=[])
        redis.store[wd._ASK_HEARTBEAT_KEY] = (
            dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat()
        result = wd.scan_failed_jobs()
        assert result["ask_stale"] is True
        assert result["heartbeat_stale"] is False
        assert len(sent) == 1
        assert "slow to answer" in sent[0]["text"]

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


class TestStudentNoteDedup:
    """Dual maintenance runners + persistent stale conditions used to send
    the same student note 2x/hour. One anchor per note-type per hour."""

    def _stale_ask(self, monkeypatch):
        import datetime as dt

        redis, sent = _wire(monkeypatch, [], seen=[])
        redis.store[wd._ASK_HEARTBEAT_KEY] = (
            dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat()
        return redis, sent

    def test_second_scan_same_hour_is_quiet(self, monkeypatch) -> None:
        _, sent = self._stale_ask(monkeypatch)
        assert wd.scan_failed_jobs()["ask_stale"] is True
        assert len(_student_texts(sent)) == 1
        sent.clear()
        assert wd.scan_failed_jobs()["ask_stale"] is True
        assert _student_texts(sent) == []  # anchored — no double-send

    def test_anchor_is_per_note_type(self, monkeypatch) -> None:
        import datetime as dt

        redis, sent = self._stale_ask(monkeypatch)
        redis.store["pia:worker:heartbeat"] = (
            dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat()
        wd.scan_failed_jobs()
        texts = _student_texts(sent)
        assert len(texts) == 2  # worker note + ask note, one each
        assert any("may be delayed" in t for t in texts)
        assert any("slow to answer" in t for t in texts)

    def test_anchor_fail_open_on_redis_blip(self) -> None:
        class _Broken:
            def set(self, *a, **k):
                raise ConnectionError("redis down")

        assert wd._note_already_sent(_Broken(), "ask") is False

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


class TestEvolutionMonitor:
    def _evo_wire(self, monkeypatch, jobs, get_handler, base="http://evo:8080",
                  key="k", **wire_kw):
        import httpx

        if get_handler is not None:
            monkeypatch.setattr(
                wd.httpx, "get",
                lambda *a, **k: httpx.Client(
                    transport=httpx.MockTransport(get_handler)).get(*a, **k))
        redis, sent = _wire(monkeypatch, jobs, **wire_kw)
        settings = _settings()
        settings.evolution_base_url = base
        settings.evolution_api_key = key
        monkeypatch.setattr(wd, "get_settings", lambda: settings)
        return redis, sent

    def test_open_instance_is_quiet(self, monkeypatch) -> None:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith(
                "/instance/connectionState/pia")
            assert request.headers["apikey"] == "k"
            return httpx.Response(200, json={"instance": {"state": "open"}})

        _, sent = self._evo_wire(monkeypatch, [], handler, seen=[])
        result = wd.scan_failed_jobs()
        assert result["evolution"] == "open"
        assert sent == []

    def test_closed_instance_pages_student_plainly(
            self, monkeypatch) -> None:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"instance": {"state": "close"}})

        _, sent = self._evo_wire(monkeypatch, [], handler, seen=[])
        result = wd.scan_failed_jobs()
        assert result["evolution"] == "close"
        assert len(sent) == 1
        assert "paused" in sent[0]["text"]
        assert "connectionState" not in sent[0]["text"]
        assert "Traceback" not in sent[0]["text"]

    def test_unreachable_stays_quiet(self, monkeypatch) -> None:
        def boom(*a, **k):
            raise ConnectionError("blip")

        _, sent = self._evo_wire(monkeypatch, [], None, seen=[])
        monkeypatch.setattr(wd.httpx, "get", boom)
        assert wd.scan_failed_jobs()["evolution"] == "unknown"
        assert sent == []

    def test_unconfigured_is_unknown(self) -> None:
        import pia_worker.jobs.watchdog as _wd

        assert _wd.evolution_connection_state()[0] in ("unknown", "open",
                                                       "close")


class TestLlmHealth:
    def _llm(self, monkeypatch, rows):

        class _FakeResult:
            def mappings(self):
                return self

            def all(self):
                return rows

        class _FakeConn:
            def execute(self, *a, **k):
                return _FakeResult()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _FakeEngine:
            def connect(self):
                return _FakeConn()

        import pia_worker.db as db
        monkeypatch.setattr(db, "engine_for_current_host",
                            lambda: _FakeEngine())

    def test_dead_provider_pages_admin_only(self, monkeypatch) -> None:
        self._llm(monkeypatch, [{"provider": "nvidia_nim",
                                 "model": "m", "bad": 5, "good": 0}])
        _, sent = _wire(monkeypatch, [], seen=[])
        # admin configured: detail section goes to admin chat
        import pia_worker.jobs.watchdog as _wd
        monkeypatch.setattr(_wd, "get_settings",
                            lambda: _settings_with_admin("-999"))
        wd.scan_failed_jobs()
        admin_texts = [s["text"] for s in sent if "chat_id" in s]
        assert len(admin_texts) == 1
        assert "nvidia_nim" in admin_texts[0]
        student_texts = [s["text"] for s in sent if "chat_id" not in s]
        assert student_texts == []

    def test_flaky_provider_stays_in_logs(self, monkeypatch) -> None:
        self._llm(monkeypatch, [{"provider": "nvidia_nim",
                                 "model": "m", "bad": 2, "good": 9}])
        _, sent = _wire(monkeypatch, [], seen=[])
        wd.scan_failed_jobs()
        assert sent == []

    def test_db_trouble_is_silent(self, monkeypatch) -> None:
        import pia_worker.db as db
        monkeypatch.setattr(
            db, "engine_for_current_host",
            lambda: (_ for _ in ()).throw(ConnectionError("down")))
        _, sent = _wire(monkeypatch, [], seen=[])
        wd.scan_failed_jobs()
        assert sent == []


def _settings_with_admin(admin: str):
    from types import SimpleNamespace
    return SimpleNamespace(
        watch_queue_depth_threshold=300, watch_heartbeat_stale_seconds=300,
        admin_telegram_chat_id=admin, debug_notifications_enabled=False,
        evolution_base_url="", evolution_api_key="",
        evolution_instance_name="pia")
