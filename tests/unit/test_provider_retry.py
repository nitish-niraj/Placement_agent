"""Transient 429/5xx retries: same-attempt sleep+retry without consuming the
single validation-repair budget; persistent 500s still fall back cleanly."""

import httpx
import pytest
from pydantic import BaseModel

from pia_worker.ai.provider import NIMProvider, ProviderError, _transient_error


class _S(BaseModel):
    value: str


def _provider(monkeypatch: pytest.MonkeyPatch, responses: list) -> NIMProvider:
    monkeypatch.setattr("pia_worker.ai.provider._log_ai_call", lambda *a, **k: None)
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        idx = len(calls)
        calls.append(idx)
        item = responses[min(idx, len(responses) - 1)]
        if isinstance(item, int):
            return httpx.Response(item, json={})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": item}}], "usage": {}})

    provider = NIMProvider(client=httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://test"))
    provider._calls = calls  # type: ignore[attr-defined]
    return provider


def _run(provider: NIMProvider):
    return provider.complete_structured(
        task="t", system="s", user="u", schema=_S, correlation_id="")


def test_transient_error_classification() -> None:
    def status_error(code: int) -> httpx.HTTPStatusError:
        req = httpx.Request("POST", "http://test/chat/completions")
        return httpx.HTTPStatusError("x", request=req,
                                     response=httpx.Response(code, json={}))

    for code in (408, 425, 429, 500, 502, 503, 504):
        assert _transient_error(status_error(code)) is True, code
    assert _transient_error(status_error(400)) is False
    assert _transient_error(status_error(401)) is False
    assert _transient_error(httpx.ConnectError("dns")) is True
    assert _transient_error(ValueError("bad json")) is False


def test_500_then_200_succeeds_without_repair(
        monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("pia_worker.ai.provider.time.sleep", sleeps.append)
    provider = _provider(monkeypatch, [500, '{"value": "ok"}'])
    parsed, usage = _run(provider)
    assert parsed.value == "ok"
    assert usage.validation == "ok"  # same attempt — not "repaired"
    assert len(provider._calls) == 2  # type: ignore[attr-defined]
    assert len(sleeps) == 1 and 2.0 <= sleeps[0] <= 3.0


def test_persistent_500_raises_after_bounded_sleeps(
        monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("pia_worker.ai.provider.time.sleep", sleeps.append)
    provider = _provider(monkeypatch, [503])
    with pytest.raises(ProviderError):
        _run(provider)
    # Shared transient budget across the call: 2 sleeps total (~11s max),
    # then the validation-repair attempt fails fast without more sleeping.
    assert len(provider._calls) == 4  # type: ignore[attr-defined]
    assert len(sleeps) == 2
    assert 2.0 <= sleeps[0] <= 3.0 and 8.0 <= sleeps[1] <= 9.0


def test_400_never_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("pia_worker.ai.provider.time.sleep", sleeps.append)
    provider = _provider(monkeypatch, [400])
    with pytest.raises(ProviderError):
        _run(provider)
    assert sleeps == []
    assert len(provider._calls) == 2  # type: ignore[attr-defined]
