"""DEC-010 fallback brains, generalized as plain-text chat helpers.

The Teams listener owns transcript-specific copies (`_openrouter_summary` /
`_groq_summary`) and stays untouched — it is live-verified. This module is the
parameterized version for every other ladder consumer (first: Ask PIA): the
system prompt and token budget travel with the caller, the transport and
error-normalization live here. Every failure mode — missing key, HTTP error,
timeout, empty content — becomes ProviderError so a ladder treats all rungs
uniformly."""

import httpx

from pia_worker.ai.provider import ProviderError
from pia_worker.settings import get_settings


def openrouter_chat(system: str, user: str, *, max_tokens: int = 1500) -> str:
    """Rung-2 brain: OpenRouter free tier (settings.openrouter_model)."""
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise ProviderError("openrouter: OPENROUTER_API_KEY not configured")
    try:
        response = httpx.post(
            f"{settings.openrouter_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "HTTP-Referer": "https://github.com/nitish-niraj/Placement_agent",
                "X-Title": "PIA fallback chain",
            },
            json={
                "model": settings.openrouter_model,
                # Reasoning models spend budget on the thinking block; a tight
                # cap leaves content=null (hit live 2026-09-13, see listener).
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        content = (response.json()["choices"][0]["message"]["content"]
                   or "").strip()
    except Exception as exc:  # noqa: BLE001 — normalized into ProviderError
        raise ProviderError(f"openrouter: {str(exc)[:180]}") from exc
    if not content:
        raise ProviderError("openrouter: empty completion")
    return content


def groq_chat(system: str, user: str, *, max_tokens: int = 1500) -> str:
    """Rung-3 brain: Groq free tier (settings.groq_model, 1K req/200K tok/day)."""
    settings = get_settings()
    if not settings.groq_api_key:
        raise ProviderError("groq: GROQ_API_KEY not configured")
    try:
        response = httpx.post(
            f"{settings.groq_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.groq_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.groq_model,
                # Groq reasoning models: max_completion_tokens needs headroom
                # or `content` returns null (see listener _groq_summary).
                "max_completion_tokens": max_tokens,
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        content = (response.json()["choices"][0]["message"]["content"]
                   or "").strip()
    except Exception as exc:  # noqa: BLE001 — normalized into ProviderError
        raise ProviderError(f"groq: {str(exc)[:180]}") from exc
    if not content:
        raise ProviderError("groq: empty completion")
    return content
