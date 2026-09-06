"""NVIDIA NIM provider — OpenAI-compatible structured completion (DEC-006).

Contract (TRD §4.1):
- One method: `complete_structured(task, system, user, schema)` -> (BaseModel, usage).
- Output is validated against the pydantic schema; on invalid output the call is
  retried once with the validation error appended (repair attempt).
- On second failure -> ProviderError; callers fall back to deterministic paths.
- Every attempt is logged to `ai_call_logs` (provider, model, latency, tokens,
  validation result, correlation id) — never message content (§14, DEC-006).
"""

import datetime as dt
import json
import re
import time
import uuid
from typing import Any, TypeVar

import httpx
import sqlalchemy
import structlog
from pydantic import BaseModel, ValidationError

from pia_worker.settings import get_settings

logger = structlog.get_logger()
T = TypeVar("T", bound=BaseModel)

_THINK = re.compile(r"<think>.*?</think>", flags=re.S)


class ProviderError(RuntimeError):
    """LLM could not produce schema-valid output within retries."""


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    validation: str  # ok | repaired | invalid
    call_id: str


def _log_ai_call(correlation_id: str, task: str, model: str, usage: Usage) -> None:
    try:
        engine = sqlalchemy.create_engine(
            get_settings().database_url, pool_pre_ping=True,
            connect_args={"connect_timeout": 3},
        )
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO ai_call_logs (correlation_id, task, provider, model, "
                    "latency_ms, prompt_tokens, completion_tokens, validation) "
                    "VALUES (CAST(:c AS uuid), :t, 'nvidia_nim', :m, :lat, :pt, :ct, :v)"
                ),
                {"c": correlation_id or str(uuid.uuid4()), "t": task, "m": model,
                 "lat": usage.latency_ms, "pt": usage.prompt_tokens,
                 "ct": usage.completion_tokens, "v": usage.validation},
            )
        engine.dispose()
    except Exception:  # noqa: BLE001 — logging must never break the pipeline
        logger.warning("ai_call_log_write_failed", task=task)


def _extract_json(content: str) -> Any:
    text = _THINK.sub("", content).strip()
    if text.startswith("```"):  # fenced JSON
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in model output")
    decoder = json.JSONDecoder()
    return decoder.raw_decode(text[start:])[0]


class NIMProvider:
    """Provider-agnostic structured completion against an OpenAI-compatible API."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        settings = get_settings()
        self._client = client or httpx.Client(
            base_url=settings.nvidia_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.nvidia_api_key}",
                     "Content-Type": "application/json"},
            timeout=settings.llm_timeout_seconds,
        )
        self._model = settings.llm_model

    def complete_structured(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema: type[T],
        correlation_id: str = "",
        max_tokens: int = 500,
    ) -> tuple[T, Usage]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self._run(task, schema, messages, correlation_id, max_tokens)

    def complete_vision_structured(
        self,
        *,
        task: str,
        system: str,
        user: str,
        image_png: bytes,
        schema: type[T],
        correlation_id: str = "",
        max_tokens: int = 1200,
    ) -> tuple[T, Usage]:
        """Vision path (FR-ELG-004): image + text -> schema. Uses LLM_VISION_MODEL."""
        import base64

        settings = get_settings()
        data_url = "data:image/png;base64," + base64.b64encode(image_png).decode()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ]
        self._model = settings.llm_vision_model
        try:
            return self._run(task, schema, messages, correlation_id, max_tokens)
        finally:
            self._model = settings.llm_model  # restore text model

    def _run(
        self,
        task: str,
        schema: type[T],
        messages: list[dict[str, Any]],
        correlation_id: str,
        max_tokens: int,
    ) -> tuple[T, Usage]:
        usage = Usage(prompt_tokens=0, completion_tokens=0, latency_ms=0,
                      validation="invalid", call_id=str(uuid.uuid4()))
        last_error: Exception | None = None

        for attempt in (1, 2):  # one repair retry (TRD §4.1)
            t0 = time.monotonic()
            try:
                response = self._client.post(
                    "/chat/completions",
                    json={"model": self._model, "messages": messages,
                          "temperature": 0.1, "max_tokens": max_tokens},
                )
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last_error = exc
                _log_ai_call(correlation_id, task, self._model, usage)
                continue

            choice = body["choices"][0]["message"]["content"]
            usage.prompt_tokens = body.get("usage", {}).get("prompt_tokens", 0)
            usage.completion_tokens = body.get("usage", {}).get("completion_tokens", 0)
            usage.latency_ms = int((time.monotonic() - t0) * 1000)

            try:
                parsed_any = _extract_json(choice)
                parsed = schema.model_validate(parsed_any)
                usage.validation = "ok" if attempt == 1 else "repaired"
                _log_ai_call(correlation_id, task, self._model, usage)
                return parsed, usage
            except (ValueError, ValidationError) as exc:
                last_error = exc
                messages.append({"role": "assistant", "content": choice})
                messages.append({
                    "role": "user",
                    "content": f"Your output failed validation: {exc}. "
                               "Return ONLY corrected JSON matching the schema.",
                })
                _log_ai_call(correlation_id, task, self._model, usage)

        raise ProviderError(f"{task}: no schema-valid output after retries: {last_error}")


def utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()
