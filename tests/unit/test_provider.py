"""Provider contract: schema validation, repair retry, ai_call logging (TRD §4.1)."""

import json

import httpx
import pytest

from pia_shared.schemas import Classification
from pia_worker.ai.provider import NIMProvider, ProviderError, _extract_json


def _client(handler) -> NIMProvider:
    transport = httpx.MockTransport(handler)
    return NIMProvider(client=httpx.Client(transport=transport, base_url="http://test"))


def _completion(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }


GOOD = json.dumps({
    "domain": "PLACEMENT", "importance": "CRITICAL",
    "confidence": 0.95, "rationale": "deadline today",
})


def test_valid_json_is_ok() -> None:
    provider = _client(lambda req: httpx.Response(200, json=_completion(GOOD)))
    result, usage = provider.complete_structured(
        task="test", system="s", user="u", schema=Classification, correlation_id="x"
    )
    assert result.domain.value == "PLACEMENT"
    assert usage.validation == "ok"


def test_invalid_then_repaired(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        payload = "sorry I cannot" if calls["n"] == 1 else GOOD
        return httpx.Response(200, json=_completion(payload))

    monkeypatch.setattr("pia_worker.ai.provider._log_ai_call", lambda *a, **k: None)
    result, usage = _client(handler).complete_structured(
        task="test", system="s", user="u", schema=Classification
    )
    assert calls["n"] == 2
    assert usage.validation == "repaired"
    assert result.domain.value == "PLACEMENT"


def test_double_failure_raises_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pia_worker.ai.provider._log_ai_call", lambda *a, **k: None)
    provider = _client(lambda req: httpx.Response(200, json=_completion("no json here")))
    with pytest.raises(ProviderError):
        provider.complete_structured(
            task="test", system="s", user="u", schema=Classification
        )


def test_schema_rejects_invented_importance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Null-when-unknown policy: only enum values pass validation (ADR-004)."""
    bad = json.dumps({"domain": "MADE_UP", "importance": "EXTREME",
                      "confidence": 2, "rationale": "x"})
    monkeypatch.setattr("pia_worker.ai.provider._log_ai_call", lambda *a, **k: None)
    provider = _client(lambda req: httpx.Response(200, json=_completion(bad)))
    with pytest.raises(ProviderError):
        provider.complete_structured(
            task="test", system="s", user="u", schema=Classification
        )


def test_extract_json_handles_fences_and_think() -> None:
    fenced = (
        '```json\n{"domain":"PLACEMENT","importance":"HIGH",'
        '"confidence":0.8,"rationale":"r"}\n```'
    )
    assert _extract_json(fenced)["domain"] == "PLACEMENT"
    thinking = (
        '<think>hmm</think>{"domain":"ACADEMIC","importance":"LOW",'
        '"confidence":0.5,"rationale":"r"}'
    )
    assert _extract_json(thinking)["domain"] == "ACADEMIC"


def test_extract_json_rejects_no_object() -> None:
    with pytest.raises(ValueError):
        _extract_json("I am not sure what you mean")
