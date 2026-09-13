"""ADR-011 Stage 1 loop, fully offline: scripted provider steps drive the
bounded ReAct loop — tool sequencing, citation filtering to observed ids,
self-correction after an unknown tool, step-budget exhaustion and NIM failure
both degrading to the P12 ladder, and best-effort trace persistence."""

import contextlib
from types import SimpleNamespace

import pytest

from pia_shared.schemas import AgentDecision, AnswerCitation
from pia_worker.agent import loop as agent_loop
from pia_worker.ai.provider import ProviderError


class FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def mappings(self):
        return self

    def all(self):
        return self._rows


class FakeConn:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.executed: list[tuple[str, dict | None]] = []

    def execute(self, sql, params=None):
        self.executed.append((str(sql), params))
        return self._results.pop(0) if self._results else FakeResult()


class FakeEngine:
    def __init__(self, conn: FakeConn):
        self.conn = conn

    def connect(self):
        return contextlib.nullcontext(self.conn)

    def begin(self):
        return contextlib.nullcontext(self.conn)


def _decision(**kw) -> AgentDecision:
    return AgentDecision(thought=kw.get("thought", "t"), **{k: v for k, v in kw.items()
                                                            if k != "thought"})


def _wire(monkeypatch: pytest.MonkeyPatch, steps: list,
          max_steps: int = 4, results: list | None = None) -> FakeConn:
    conn = FakeConn(results)
    monkeypatch.setattr(agent_loop, "get_settings",
                        lambda: SimpleNamespace(agent_max_steps=max_steps))
    monkeypatch.setattr(
        agent_loop, "NIMProvider",
        lambda: SimpleNamespace(complete_structured=lambda **kw: _next_step(steps)))
    monkeypatch.setattr(agent_loop, "engine_for_current_host",
                        lambda: FakeEngine(conn))
    return conn


def _next_step(steps: list):
    if not steps:
        raise ProviderError("script exhausted")
    step = steps.pop(0)
    if isinstance(step, Exception):
        raise step
    return step, None


class TestLoop:
    def test_tool_then_answer_with_filtered_citations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _wire(monkeypatch, [
            _decision(tool="search_messages", args={"query": "softlink"}),
            _decision(final_answer="You were eligible for SOFTLINK.",
                      citations=[AnswerCitation(kind="message", ref="m-1"),
                                 AnswerCitation(kind="message", ref="INVENTED")],
                      confidence=0.8),
        ], results=[FakeResult(rows=[{"id": "m-1", "text": "eligible for SOFTLINK",
                                      "group_name": "g", "sent_at": "now"}])])
        result = agent_loop.ask_agent("was i eligible for the softlink?")
        assert result["source"] == "agent"
        assert result["answer"] == "You were eligible for SOFTLINK."
        assert [c["ref"] for c in result["citations"]] == ["m-1"]  # invented dropped
        assert [s["tool"] for s in result["steps"]] == ["search_messages", None]
        assert any("agent_traces" in sql for sql, _ in conn.executed)  # trace persisted

    def test_unknown_tool_is_an_observation_then_recovers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, [
            _decision(tool="weather_forecast", args={}),
            _decision(final_answer="Answering from what I have."),
        ])
        result = agent_loop.run_agent("q?")
        assert [s["tool"] for s in result["steps"]] == ["weather_forecast", None]

    def test_empty_step_is_nudged_not_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A step with neither tool nor answer gets a corrective nudge (bounded);
        the live model produced exactly this once (2026-09-13)."""
        _wire(monkeypatch, [
            _decision(thought="thinking"),
            _decision(final_answer="Done."),
        ])
        result = agent_loop.ask_agent("q?")
        assert result["source"] == "agent"
        assert len(result["steps"]) == 1  # the empty step leaves no trace record

    def test_budget_exhaustion_degrades_to_p12(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _wire(monkeypatch, [
            _decision(tool="search_messages", args={"query": "x"}),
            _decision(tool="search_messages", args={"query": "y"}),
        ], max_steps=2)
        sent: list = []

        def fake_ask(q: str) -> dict:
            sent.append(q)
            return {"answer": "fallback answer", "citations": [],
                    "says_unavailable": False, "confidence": 0.6,
                    "fallback": True, "source": "deterministic"}

        monkeypatch.setattr(agent_loop, "ask_question", fake_ask)
        result = agent_loop.ask_agent("q?")
        assert result["source"] == "p12_fallback"
        assert result["answer"] == "fallback answer"
        assert result["agent_degraded"] is True
        assert sent == ["q?"]
        assert any("agent_traces" in sql for sql, _ in conn.executed)

    def test_nim_failure_degrades_to_p12(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, [ProviderError("nim down")])
        monkeypatch.setattr(agent_loop, "ask_question",
                            lambda q: {"answer": "fb", "citations": [],
                                       "says_unavailable": False,
                                       "confidence": 0.6, "fallback": True,
                                       "source": "deterministic"})
        result = agent_loop.ask_agent("q?")
        assert result["source"] == "p12_fallback"
        assert result["answer"] == "fb"


class TestScratchpad:
    def test_capped_to_budget(self) -> None:
        history = ["x" * 5000] * 5
        assert len(agent_loop._scratchpad("q?", history)) <= 12000 + 20

    def test_observation_capped(self) -> None:
        blob = {"rows": ["y" * 5000]}
        assert len(agent_loop._observation(blob)) <= 1500
