"""DEC-010 summary fallback chain: NIM -> OpenRouter -> Groq -> raw.

Offline by construction: every network rung is monkeypatched, asserting the
LADDER ORDER and the honest final fallback (source-backed raw transcript).
"""
import pia_worker.teams.listener as L
from pia_worker.ai.provider import ProviderError


class _DeadNIM:
    """Stands in for NIMProvider().complete_structured -> ProviderError."""

    def complete_structured(self, **_kw):
        raise ProviderError("nim down")


def _raise(msg: str):
    def _f(_prompt: str) -> str:
        raise ProviderError(msg)
    return _f


def test_nim_failure_lands_on_openrouter(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(L, "NIMProvider", _DeadNIM)
    monkeypatch.setattr(L, "_groq_summary", _raise("groq should not run"))
    monkeypatch.setattr(
        L, "_openrouter_summary",
        lambda p: calls.append("openrouter") or "OR summary")
    out = L._summarize("This is Vijay from TCS.", None)
    assert calls == ["openrouter"]
    assert "via OpenRouter" in out


def test_openrouter_failure_lands_on_groq(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(L, "NIMProvider", _DeadNIM)
    monkeypatch.setattr(L, "_openrouter_summary", _raise("or down"))
    monkeypatch.setattr(
        L, "_groq_summary",
        lambda p: calls.append("groq") or "Groq summary")
    out = L._summarize("some transcript", "https://forms.example/x")
    assert calls == ["groq"]
    assert "via Groq" in out
    assert "forms.example/x" in out  # form link rides every rung


def test_all_brains_dead_falls_back_to_raw(monkeypatch):
    monkeypatch.setattr(L, "NIMProvider", _DeadNIM)
    monkeypatch.setattr(L, "_openrouter_summary", _raise("or down"))
    monkeypatch.setattr(L, "_groq_summary", _raise("groq down"))
    out = L._summarize("actual words spoken in the session", None)
    assert "raw transcript" in out
    assert "actual words spoken" in out  # never invented, always sourced
