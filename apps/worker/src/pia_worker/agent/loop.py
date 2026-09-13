"""ADR-011 Stage 1 — the bounded ReAct loop (tool-using answer agent).

One question → up to AGENT_MAX_STEPS schema-validated model steps; each step
either calls one read-only tool (tools.py) or produces the final answer with
citations that must reference message ids actually observed in tool results.
The deterministic core is untouched and every failure degrades: a NIM error
(or a dead end) at any point falls back to the P12 single-shot ladder
(ask_question), which has its own deterministic end rung. Full traces are
persisted to agent_traces (best-effort — persistence never breaks an answer).
"""

import json
from collections.abc import Callable

import sqlalchemy
import structlog

from pia_shared.schemas import AgentDecision
from pia_worker.agent import tools
from pia_worker.ai.provider import NIMProvider, ProviderError
from pia_worker.db import engine_for_current_host
from pia_worker.search.answer import ask_question
from pia_worker.settings import get_settings

logger = structlog.get_logger()

_OBSERVATION_CAP = 1500
_SCRATCHPAD_CAP = 12000

AGENT_SYSTEM = """You are PIA's placement agent, answering for ONE student.
You have read-only tools over the student's stored placement data.
Rules:
- Think step by step. Call a tool when you need facts; several small calls
  beat one vague one. Use search_messages for anything you might need to
  cite; the other tools give structured facts.
- Answer ONLY from tool results. Never invent companies, dates, numbers,
  or message texts. If the tools cannot answer the question, say exactly
  what is missing instead of guessing.
- When you can answer, set final_answer (under 150 words, plain text) and
  put every message id you actually used into citations (kind "message",
  ref = that message's id). No citations for purely structured-fact answers.
- When the question needs no more tools, answer immediately — do not call
  tools for show.
Available tools:
{tool_docs}"""


def _tool_docs(specs: dict[str, str] | None = None) -> str:
    return "\n".join(f"- {name}: {desc}"
                     for name, desc in (specs or tools.TOOL_SPECS).items())


def _observation(result) -> str:
    return json.dumps(result, default=str, ensure_ascii=False)[:_OBSERVATION_CAP]


def _scratchpad(question: str, history: list[str]) -> str:
    text = f"QUESTION: {question}\n\n" + "\n".join(history)
    return text[-_SCRATCHPAD_CAP:]


def run_agent(question: str, *, system: str | None = None,
              extra_tools: dict[str, tuple[str, Callable[..., object]]] | None = None,
              max_steps: int | None = None) -> dict:
    """The bounded loop. Returns {answer, citations, steps, source, confidence,
    fallback}; source == "agent" only when the loop answered. Raises
    ProviderError on model failure or dead end — ask_agent degrades.

    Stage 2 hooks: `system` replaces the default ask prompt, `extra_tools`
    maps tool name -> (description, handler(args_dict)) — the handler runs in
    place of the read-only registry (this is how the reviewer proposes), and
    `max_steps` overrides the configured budget."""
    settings = get_settings()
    max_steps = max(1, max_steps if max_steps is not None
                    else settings.agent_max_steps)
    specs = dict(tools.TOOL_SPECS)
    handlers: dict[str, Callable[..., object]] = {}
    for name, (desc, handler) in (extra_tools or {}).items():
        specs[name] = desc
        handlers[name] = handler
    base = system or AGENT_SYSTEM
    # replace, not .format() — custom prompts may contain JSON braces
    system_text = base.replace("{tool_docs}", _tool_docs(specs))
    provider = NIMProvider()
    engine = engine_for_current_host()
    history: list[str] = []
    steps: list[dict] = []
    seen_ids: set[str] = set()

    def _dispatch(name: str, args: dict[str, str]):
        if name in handlers:
            return handlers[name](**args)
        return tools.run_tool(conn, name, args)

    with engine.connect() as conn:
        for step_no in range(1, max_steps + 1):
            decision: AgentDecision = provider.complete_structured(
                task="agent_step",
                system=system_text,
                user=_scratchpad(question, history),
                schema=AgentDecision,
                correlation_id="",
                max_tokens=400,
            )[0]
            record: dict = {"step": step_no, "thought": decision.thought[:200],
                            "tool": decision.tool, "args": dict(decision.args)}
            if decision.final_answer:
                citations = [c for c in decision.citations
                             if str(c.ref) in seen_ids]
                steps.append(record)
                return {
                    "answer": decision.final_answer,
                    "citations": [c.model_dump(mode="json") for c in citations],
                    "steps": steps,
                    "source": "agent",
                    "confidence": decision.confidence,
                    "says_unavailable": False,
                    "fallback": False,
                }
            if not decision.tool and not (decision.final_answer or "").strip():
                # Transient model quirk — nudge instead of dying: the loop is
                # bounded, so one corrective step is cheap and self-heals.
                if step_no >= max_steps:
                    raise ProviderError("agent: last step had no tool and no answer")
                history.append(
                    "SYSTEM: your previous step had neither a tool call nor a "
                    "final_answer. Either call one of the listed tools or set "
                    "final_answer now.")
                continue
            if not decision.tool:
                raise ProviderError(f"agent step {step_no}: no tool, no answer")
            result = _dispatch(decision.tool, dict(decision.args))
            if isinstance(result, list):
                for row in result:
                    if isinstance(row, dict):
                        if row.get("id"):
                            seen_ids.add(str(row["id"]))
                        if row.get("source_message_id"):
                            seen_ids.add(str(row["source_message_id"]))
            observation = _observation(result)
            record["observation_chars"] = len(observation)
            steps.append(record)
            history.append(f"THOUGHT: {decision.thought[:200]}")
            history.append(f"TOOL {decision.tool}({json.dumps(decision.args)})")
            history.append(f"OBSERVATION: {observation}")
    raise ProviderError(f"agent: no final answer within {max_steps} steps")


def _persist_trace(question: str, result: dict) -> None:
    try:
        engine = engine_for_current_host()
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO agent_traces (question, source, answer, "
                    "citations, steps) VALUES (:q, :src, :ans, "
                    "CAST(:cit AS jsonb), CAST(:steps AS jsonb))"
                ),
                {"q": question[:500], "src": str(result.get("source")),
                 "ans": str(result.get("answer"))[:4000],
                 "cit": json.dumps(result.get("citations", []), default=str),
                 "steps": json.dumps(result.get("steps", []), default=str)},
            )
    except Exception as exc:  # noqa: BLE001 — observability never breaks answers
        logger.warning("agent_trace_persist_failed", error=str(exc)[:120])


def ask_agent(question: str) -> dict:
    """API entry: the agent loop with the P12 ladder as the degradation
    fallback. Never raises to the caller; the trace is persisted best-effort
    either way (ADR-011: agent at the edges, pipeline at the core)."""
    try:
        result = run_agent(question)
        _persist_trace(question, result)
        return result
    except Exception as exc:  # noqa: BLE001 — degradation, never failure
        logger.warning("agent_fallback_to_p12", error=str(exc)[:120])
        fallback = dict(ask_question(question))
        fallback["source"] = "p12_fallback"
        fallback["agent_degraded"] = True
        fallback["fallback"] = True
        _persist_trace(question, fallback)
        return fallback
