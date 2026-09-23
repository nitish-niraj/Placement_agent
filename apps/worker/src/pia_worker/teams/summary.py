"""KYC session summarization (strangler extract from listener.py, 2026-09-23).

DEC-010 ladder, verbatim: NIM structured -> OpenRouter -> Groq -> honest raw
transcript. Zero Playwright — the first extracted module, unit-testable.
listener.py re-exports these names so existing imports keep working.
"""

from pathlib import Path

import httpx
import structlog

from pia_shared.schemas import MeetingSummary
from pia_worker.ai.provider import NIMProvider, ProviderError
from pia_worker.settings import get_settings

logger = structlog.get_logger()


def _openrouter_summary(prompt: str) -> str:
    """Second brain when NIM fails (owner decision 2026-09-12): OpenRouter
    chat completion, plain text — deliberately simpler than the structured
    primary path. Best-effort: raises ProviderError for the caller's final
    raw-transcript fallback. NOTE: the free OpenRouter balance is small, so
    the transcript cap here is tighter than the NIM prompt's."""
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise ProviderError("openrouter: OPENROUTER_API_KEY not configured")
    try:
        response = httpx.post(
            f"{settings.openrouter_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}",
                     "HTTP-Referer": "https://github.com/nitish-niraj/Placement_agent",
                     "X-Title": "PIA KYC summary fallback"},
            json={
                "model": settings.openrouter_model,
                # ling-3.0-flash-vl is a REASONING model: a small budget can
                # be consumed entirely by its thinking block, leaving
                # content=null (hit live in manual testing 2026-09-13).
                "max_tokens": 1500,
                "messages": [
                    {"role": "system", "content": (
                        "Summarize placement KYC session transcripts for the "
                        "student: company, role, package if mentioned, key "
                        "points. Plain text bullets, only facts from the "
                        "text, under 120 words.")},
                    {"role": "user", "content": prompt},
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


def _groq_summary(prompt: str) -> str:
    """Third brain when NIM and OpenRouter both fail (DEC-010): Groq's free
    tier `openai/gpt-oss-120b` (1K requests / 200K tokens per day, tested
    2026-09-13). Reasoning model — max_completion_tokens must leave headroom
    or `content` returns null. Raises ProviderError for the final raw
    fallback."""
    settings = get_settings()
    if not settings.groq_api_key:
        raise ProviderError("groq: GROQ_API_KEY not configured")
    try:
        response = httpx.post(
            f"{settings.groq_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}",
                     "Content-Type": "application/json"},
            json={
                "model": settings.groq_model,
                "max_completion_tokens": 1500,
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": (
                        "Summarize placement KYC session transcripts for the "
                        "student: company, role, package if mentioned, key "
                        "points. Plain text bullets, only facts from the "
                        "text, under 120 words.")},
                    {"role": "user", "content": prompt},
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


def _summarize(transcript_text: str, form_link: str | None,
               presenters: tuple[str, ...] = ()) -> str:
    """LLM summary of the discussion from the captured captions; deterministic
    fallback returns the raw transcript (source-backed, never invented).
    `presenters` = names from caption self-introductions (the LLM only picks
    among them — never invents)."""
    who = ("Name candidates for the recruiter/teacher who led the session: "
           + ", ".join(presenters) + ". Say who led it if the text shows it. "
           if presenters else "")
    prompt = (
        "This is the live-captions transcript of a company KYC information "
        "session. Summarize for the student: the company, the designation/role "
        "discussed, package/salary if mentioned, and key points. " + who +
        "Use only this text.\n\nTRANSCRIPT:\n" + transcript_text[:12000]
    )
    try:
        summary: MeetingSummary = NIMProvider().complete_structured(
            task="kyc_session_summary", system=(
                "Summarize placement KYC session transcripts. Fill every field "
                "from the transcript only; leave fields null when not discussed. "
                "key_points = short bullets. summary = under 120 words."
            ),
            user=prompt, schema=MeetingSummary, max_tokens=500,
        )[0]
        lines = [f"🏢 {summary.company or 'Company session'} — summary"]
        if summary.designation_discussed:
            lines.append(f"💼 Role discussed: {summary.designation_discussed}")
        if summary.package_mentioned:
            lines.append(f"💰 Package: {summary.package_mentioned}")
        lines.extend(f"• {point}" for point in summary.key_points)
        lines.append(summary.summary)
        if form_link:
            lines.append(f"📝 Feedback form: {form_link}")
        return "\n".join(lines)
    except ProviderError as exc:
        logger.warning("summary_llm_unavailable", error=str(exc)[:120])
        # Fallback ladder (DEC-010, owner order 2026-09-13):
        # NIM -> OpenRouter -> Groq -> raw transcript. Each rung tags itself
        # in the message, so you always know which brain answered.
        # NB: `exc`/`exc2` are deleted at the end of their except blocks in
        # py3, so persist the reason strings in plain locals to reuse below.
        nim_error = str(exc)
        or_error = ""
        or_prompt = ("TRANSCRIPT:\n" + transcript_text[:4000])
        try:
            text = _openrouter_summary(or_prompt)
            logger.info("summary_openrouter_fallback_used")
            if form_link:
                text += f"\n📝 Feedback form: {form_link}"
            return text + "\n🤖 via OpenRouter (primary NIM failed: " \
                + nim_error[:60] + ")"
        except ProviderError as exc2:
            or_error = str(exc2)
            logger.warning("summary_openrouter_unavailable",
                           error=or_error[:160])
        try:
            text = _groq_summary(or_prompt)
            logger.info("summary_groq_fallback_used")
            if form_link:
                text += f"\n📝 Feedback form: {form_link}"
            return text + "\n🤖 via Groq (NIM + OpenRouter failed: " \
                + or_error[:60] + ")"
        except ProviderError as exc3:
            logger.warning("summary_groq_unavailable",
                           error=str(exc3)[:160])
        # Final rung — honest raw transcript (source-backed, never invented).
        head = transcript_text[:1500]
        tail = f"\n\n📝 Feedback form: {form_link}" if form_link else ""
        return (f"LLM unavailable — raw transcript (first {len(head)} chars):\n"
                f"{head}{tail}")


def summarize_transcript_file(path: str | Path, form_link: str | None = None,
                              presenters: tuple[str, ...] = ()) -> str:
    """Summarize a saved transcript file (pure: file in, text out). Mirrors
    the listener tail: empty file -> "no captions captured", else the ladder
    summary prefixed with the presenter line when names are known."""
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return "no captions captured"
    summary = _summarize(text, form_link, presenters)
    if presenters:
        summary = f"👤 Presenter: {', '.join(presenters)}\n" + summary
    return summary
