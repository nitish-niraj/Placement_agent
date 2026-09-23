"""Attachment recovery: missing bytes fail fast with codes (no DLQ spam),
transports retry with backoff, Evolution refetch recovers, stuck rows reap.

Covers the reported `download_attachment × PermanentJobError: media content
missing` failure end-to-end at the unit level (DB/MinIO/network all faked).
"""

import base64
import contextlib
import json
from types import SimpleNamespace

import httpx
import pytest

from pia_worker.jobs import process_message as pm
from pia_worker.media_refetch import (
    RefetchUnavailable,
    extract_wa_key_id,
    fetch_base64_from_evolution,
)


class FakeResult:
    def __init__(self, mapping=None, mapping_list=None, value=None,
                 rowcount=0):
        self._mapping = mapping
        self._list = mapping_list or []
        self._value = value
        self.rowcount = rowcount

    def mappings(self):
        return self

    def first(self):
        return self._mapping

    def all(self):
        return self._list

    def scalar(self):
        return self._value

    def scalar_one(self):
        return self._value


class FakeConn:
    def __init__(self, script):
        self._script = script
        self.executed: list[tuple[str, dict | None]] = []

    def execute(self, sql, params=None):
        text = str(sql)
        self.executed.append((text, params))
        return self._script(text, params)


class FakeEngine:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    def connect(self):
        conn = self._conn

        class _Raw:
            def __enter__(self):
                return conn

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                return conn.execute(sql, params)

        return _Raw()

    def begin(self):
        return contextlib.nullcontext(self._conn)


B64 = base64.b64encode(b"csv-bytes").decode()


def _payload_with_inline() -> dict:
    return {"data": {"message": {"base64": B64,
                                 "documentMessage": {"fileName": "l.csv"}}}}


def _payload_without_bytes(key_id: str = "WAKEY123") -> dict:
    return {"data": {"message": {"key": {"id": key_id},
                                 "documentMessage": {"fileName": "l.csv"}}}}


def _wire_engine(monkeypatch: pytest.MonkeyPatch, script) -> FakeConn:
    conn = FakeConn(script)
    monkeypatch.setattr(pm, "_engine", lambda: FakeEngine(conn))
    return conn


class FakeMinio:
    def __init__(self):
        self.put: list = []

    def bucket_exists(self, bucket: str) -> bool:
        return True

    def put_object(self, bucket, key, data, length, content_type=None):
        self.put.append((bucket, key, length))


class FakeQueue:
    seen: list = []

    def __init__(self, name, connection=None):
        self.name = name

    def enqueue(self, *args, **kwargs):
        FakeQueue.seen.append((args, kwargs))
        return None


def _wire_infra(monkeypatch: pytest.MonkeyPatch) -> FakeMinio:
    minio = FakeMinio()
    monkeypatch.setattr(pm, "_minio_client", lambda: minio)
    FakeQueue.seen = []
    monkeypatch.setattr("rq.Queue", FakeQueue)
    monkeypatch.setattr("redis.Redis", FakeRedis())
    return minio


class FakeRedis:
    @staticmethod
    def from_url(url: str):
        return "redis-conn"


def _settings(monkeypatch: pytest.MonkeyPatch, **over):
    from types import SimpleNamespace as NS

    base = {"media_max_size_mb": 25, "minio_bucket": "pia-media",
            "minio_endpoint": "http://localhost:9000", "redis_url": "redis://x",
            "attachment_retry_enabled": False, "evolution_base_url": "",
            "evolution_api_key": "", "evolution_instance_name": "pia"}
    base.update(over)
    monkeypatch.setattr(pm, "get_settings", lambda: NS(**base))


class TestFetchInline:
    def test_three_locations(self) -> None:
        assert pm._fetch_media_bytes(
            {"message": {"base64": B64}}) == b"csv-bytes"
        assert pm._fetch_media_bytes(
            {"message": {"documentMessage": {"base64": B64}}}) == b"csv-bytes"
        assert pm._fetch_media_bytes({"base64": B64}) == b"csv-bytes"

    def test_missing_raises_transient_marker_not_permanent(self) -> None:
        with pytest.raises(pm._MediaMissing):
            pm._fetch_media_bytes(_payload_without_bytes())
        assert not issubclass(pm._MediaMissing, pm.PermanentJobError)


class TestDownloadOutcomes:
    def test_missing_row_returns_missing(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        _settings(monkeypatch)
        conn = _wire_engine(
            monkeypatch,
            lambda sql, p: FakeResult(mapping=None))
        assert pm.download_attachment("att-1") == "missing"
        _ = conn

    def test_no_raw_payload_fails_with_code(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        _settings(monkeypatch)
        row = SimpleNamespace(processing_state="PENDING", message_id="m",
                              mime_type="text/csv", file_name="l.csv")

        def script(sql: str, params: dict | None):
            if "FROM attachments WHERE id" in sql:
                return FakeResult(mapping=row)
            if "FROM raw_event_payloads" in sql:
                return FakeResult(mapping=None)
            return FakeResult()

        conn = _wire_engine(monkeypatch, script)
        assert pm.download_attachment("att-1") == "failed"
        updates = [p for sql, p in conn.executed
                   if "CAST('FAILED'" in sql and p]
        assert updates and updates[0]["reason"] == "no_raw_payload:unrecoverable"

    def test_missing_bytes_no_retry_config_fails_fast(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        _settings(monkeypatch)  # retry disabled, evolution unconfigured
        row = SimpleNamespace(processing_state="DOWNLOADING", message_id="m",
                              mime_type="text/csv", file_name="l.csv")
        payload = SimpleNamespace(payload=_payload_without_bytes())

        def script(sql: str, params: dict | None):
            if "FROM attachments WHERE id" in sql:
                return FakeResult(mapping=row)
            if "FROM raw_event_payloads" in sql:
                return FakeResult(mapping=payload)
            return FakeResult()

        conn = _wire_engine(monkeypatch, script)
        assert pm.download_attachment("att-1") == "failed"
        updates = [p for sql, p in conn.executed
                   if "CAST('FAILED'" in sql and p]
        assert updates[0]["reason"] == "base64_missing:unrecoverable"

    def test_refetch_recovers_to_stored(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        _settings(monkeypatch, attachment_retry_enabled=True,
                  evolution_base_url="http://evo:8080",
                  evolution_api_key="k", evolution_instance_name="pia")
        row = SimpleNamespace(processing_state="PENDING", message_id="m",
                              mime_type="text/csv", file_name="l.csv")
        payload = SimpleNamespace(payload=_payload_without_bytes())

        def script(sql: str, params: dict | None):
            if "FROM attachments WHERE id" in sql:
                return FakeResult(mapping=row)
            if "FROM raw_event_payloads" in sql:
                return FakeResult(mapping=payload)
            return FakeResult()

        _wire_engine(monkeypatch, script)
        monkeypatch.setattr(
            "pia_worker.jobs.process_message.fetch_base64_from_evolution",
            lambda **kw: b"csv-bytes")
        minio = _wire_infra(monkeypatch)
        assert pm.download_attachment("att-1") == "stored"
        assert minio.put and minio.put[0][2] == len(b"csv-bytes")

    def test_refetch_unavailable_fails_with_code(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        _settings(monkeypatch, attachment_retry_enabled=True,
                  evolution_base_url="http://evo:8080",
                  evolution_api_key="k", evolution_instance_name="pia")
        row = SimpleNamespace(processing_state="PENDING", message_id="m",
                              mime_type="text/csv", file_name="l.csv")
        payload = SimpleNamespace(payload=_payload_without_bytes())

        def script(sql: str, params: dict | None):
            if "FROM attachments WHERE id" in sql:
                return FakeResult(mapping=row)
            if "FROM raw_event_payloads" in sql:
                return FakeResult(mapping=payload)
            return FakeResult()

        conn = _wire_engine(monkeypatch, script)

        def boom(**kw):
            raise RefetchUnavailable("gone")

        monkeypatch.setattr(
            "pia_worker.jobs.process_message.fetch_base64_from_evolution",
            boom)
        assert pm.download_attachment("att-1") == "failed"
        updates = [p for sql, p in conn.executed
                   if "CAST('FAILED'" in sql and p]
        assert updates[0]["reason"] == \
            "evolution_media_not_stored:unrecoverable"


class TestRetryAndReap:
    def test_retry_requeues_failed(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        _settings(monkeypatch)

        def script(sql: str, params: dict | None):
            if "processing_state::text" in sql:
                return FakeResult(
                    mapping=SimpleNamespace(state="FAILED"))
            return FakeResult()

        _wire_engine(monkeypatch, script)
        FakeQueue.seen = []
        import pia_worker.queue as qmod
        monkeypatch.setattr(qmod, "Queue", FakeQueue)
        from pia_worker.jobs.process_message import retry_attachment
        # Queue import inside retry_attachment is pia_worker.queue.DEFAULT_QUEUE
        # + redis + rq — patch at source modules:
        _wire_infra(monkeypatch)
        assert retry_attachment("att-1") == "requeued"
        assert any("download_attachment" in str(a)
                   for a, _ in FakeQueue.seen)

    def test_retry_refuses_non_failed(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        _settings(monkeypatch)

        def script(sql: str, params: dict | None):
            if "processing_state::text" in sql:
                return FakeResult(
                    mapping=SimpleNamespace(state="PROCESSED"))
            return FakeResult()

        _wire_engine(monkeypatch, script)
        from pia_worker.jobs.process_message import retry_attachment
        assert retry_attachment("att-1") == "not_failed:PROCESSED"

    def test_reap_stuck_downloading(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        _settings(monkeypatch)

        def script(sql: str, params: dict | None):
            if "processing_state = CAST('DOWNLOADING'" in sql:
                return FakeResult(mapping_list=[{"id": "old-1"}])
            return FakeResult()

        conn = _wire_engine(monkeypatch, script)
        from pia_worker.jobs.process_message import reap_stuck_attachments
        assert reap_stuck_attachments() == {"reaped": 1}
        assert any("reaped_stuck_downloading" in sql
                   for sql, _ in conn.executed)


class TestRefetchClient:
    def _client(self, status: int, body: dict) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith(
                "/chat/getBase64FromMediaMessage/pia")
            assert request.headers["apikey"] == "k"
            sent = json.loads(request.content.decode())
            assert sent["message"]["key"]["id"] == "WAKEY"
            return httpx.Response(status, json=body)

        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_success(self) -> None:
        out = fetch_base64_from_evolution(
            base_url="http://evo:8080", api_key="k", instance="pia",
            wa_key_id="WAKEY",
            client=self._client(200, {"base64": B64}))
        assert out == b"csv-bytes"

    def test_400_is_unavailable(self) -> None:
        with pytest.raises(RefetchUnavailable):
            fetch_base64_from_evolution(
                base_url="http://evo:8080", api_key="k", instance="pia",
                wa_key_id="WAKEY", client=self._client(400, {}))

    def test_ok_false_is_unavailable(self) -> None:
        with pytest.raises(RefetchUnavailable):
            fetch_base64_from_evolution(
                base_url="http://evo:8080", api_key="k", instance="pia",
                wa_key_id="WAKEY",
                client=self._client(200, {"ok": False,
                                          "message": "not found"}))

    def test_key_extraction_shapes(self) -> None:
        assert extract_wa_key_id(
            {"message": {"key": {"id": "A"}}}) == "A"
        assert extract_wa_key_id({"key": {"id": "B"}}) == "B"
        assert extract_wa_key_id({}) is None


class TestBackoff:
    def test_download_retry_is_spaced_not_immediate(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pia_api import jobs as jobs_mod

        seen: list = []

        class Q:
            def __init__(self, name, connection=None):
                pass

            def enqueue(self, *args, **kwargs):
                seen.append(kwargs.get("retry"))
                return None

            def enqueue_in(self, *args, **kwargs):
                seen.append(kwargs.get("retry"))
                return None

        monkeypatch.setattr(jobs_mod, "Queue", Q)
        monkeypatch.setattr(jobs_mod, "Redis", FakeRedis)
        jobs_mod.enqueue_download_attachment("a")
        retry = seen[0]
        assert retry.max == 5
        assert list(retry.intervals) == [60, 300, 900, 1800, 3600]
