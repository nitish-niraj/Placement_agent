"""ADR-011 Stage 4 — reactive per-message judgment, fully offline.

The guardrails ARE the feature: flag off by default, rule veto (the judge is
never consulted for messages the rules already escalate, and it can never
downgrade), confidence threshold, daily escalation cap, and audit rows."""

from types import SimpleNamespace

import pytest

from pia_shared.schemas import ReactiveJudgment
from pia_worker.agent import reactive as rj
from pia_worker.ai.provider import ProviderError


class FakeResult:
    def __init__(self, value=None, mapping=None):
        self._v = value
        self._m = mapping

    def scalar(self):
        return self._v

    def mappings(self):
        return self

    def first(self):
        return self._m


class FakeConn:
    def __init__(self, row: dict | None = None):
        self.row = row or {}
        self.executed: list[tuple[str, dict | None]] = []

    def execute(self, sql, params=None):
        self.executed.append((str(sql), params))
        if "FROM messages" in str(sql):
            return FakeResult(mapping=self.row)
        return FakeResult()


class FakeEngine:
    def __init__(self, conn: FakeConn):
        self.conn = conn

    def connect(self):
        import contextlib
        return contextlib.nullcontext(self.conn)

    def begin(self):
        import contextlib
        return contextlib.nullcontext(self.conn)


ROW = {"text": "please be present tomorrow at 10 for the drive",
       "domain": "UNKNOWN", "importance": "MEDIUM"}

SENT: list = []


def _wire(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True,
          threshold: float = 0.7, cap: int = 3, escalations: int = 0,
          judgment: ReactiveJudgment | None = None,
          provider_error=None, row: dict | None = None) -> FakeConn:
    monkeypatch.setattr(
        rj, "get_settings",
        lambda: SimpleNamespace(
            stage4_reactive_enabled=enabled,
            stage4_confidence_threshold=threshold,
            stage4_escalation_cap=cap, redis_url="redis://x",
            app_timezone="Asia/Kolkata"))
    conn = FakeConn(row if row is not None else dict(ROW))
    monkeypatch.setattr(rj, "engine_for_current_host", lambda: FakeEngine(conn))
    if provider_error is not None:
        def fail(**kw):
            raise provider_error
        monkeypatch.setattr(rj, "NIMProvider",
                            lambda: SimpleNamespace(complete_structured=fail))
    else:
        j = judgment or ReactiveJudgment(
            verdict="escalate", reason="drive tomorrow at 10", confidence=0.9)
        monkeypatch.setattr(rj, "NIMProvider", lambda: SimpleNamespace(
            complete_structured=lambda **kw: (j, None)))
    monkeypatch.setattr(rj, "_telegram_send", lambda **kw: SENT.append(kw) or True)
    SENT.clear()  # isolation: judgments from a previous test must not leak

    counts = {"n": escalations}

    class FakeRedis:
        def get(self, key):
            return counts["n"] if counts["n"] else None

        def incr(self, key):
            counts["n"] += 1
            return counts["n"]

        def expire(self, key, ttl):
            return True

    monkeypatch.setattr("pia_worker.queue.make_connection",
                        lambda *a, **k: FakeRedis())
    return conn


class TestGates:
    def test_disabled_by_default(self, monkeypatch) -> None:
        conn = _wire(monkeypatch, enabled=False)
        assert rj.judge_message("m-1") == "disabled"
        assert not any("audit_logs" in sql for sql, _ in conn.executed)

    def test_rule_veto_already_escalated(self, monkeypatch) -> None:
        high = dict(ROW, importance="CRITICAL")
        _wire(monkeypatch, row=high)
        assert rj.judge_message("m-1") == "skipped_rule_veto"
        assert SENT == []  # the judge was never consulted


class TestJudgment:
    def test_escalate_sends_heads_up_and_audits(self, monkeypatch) -> None:
        conn = _wire(monkeypatch)
        result = rj.judge_message("m-1")
        assert result == "judged:escalate"
        assert any("Possible missed alert" in str(kw.get("text", ""))
                   for kw in SENT)
        audits = [p for sql, p in conn.executed if "audit_logs" in sql and p]
        assert audits and audits[0]["result"] == "escalate"

    def test_below_threshold_downgrades_to_digest(self, monkeypatch) -> None:
        _wire(monkeypatch, judgment=ReactiveJudgment(
            verdict="escalate", reason="drive tomorrow", confidence=0.5))
        assert rj.judge_message("m-1") == "judged:digest"
        assert not any("Possible missed alert" in str(kw.get("text", ""))
                       for kw in SENT)

    def test_digest_and_ignore_never_message(self, monkeypatch) -> None:
        for verdict in ("digest", "ignore"):
            SENT.clear()
            _wire(monkeypatch, judgment=ReactiveJudgment(
                verdict=verdict, reason="r", confidence=0.9))
            rj.judge_message("m-1")
            assert not any("Possible missed alert" in str(kw.get("text", ""))
                           for kw in SENT)

    def test_daily_cap_blocks_escalation(self, monkeypatch) -> None:
        _wire(monkeypatch, cap=3, escalations=3)
        result = rj.judge_message("m-1")
        assert result == "escalation_capped"
        assert not any("Possible missed alert" in str(kw.get("text", ""))
                       for kw in SENT)

    def test_nim_failure_degrades_silently(self, monkeypatch) -> None:
        conn = _wire(monkeypatch, provider_error=ProviderError("503"))
        assert rj.judge_message("m-1") == "judgment_unavailable"
        assert not any("audit_logs" in sql for sql, _ in conn.executed)
