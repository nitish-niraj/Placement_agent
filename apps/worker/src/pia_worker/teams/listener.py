"""Type-1 KYC personal listener (DEC-008 amendment, owner decision 2026-09-06).

Joins an INFORMATIONAL company-arrival Teams session with the owner's saved
session, turns on live captions and scrapes them into a local transcript,
watches the meeting chat for the feedback-form link, relays it to Telegram
instantly, then leaves and posts an LLM summary of the discussion. Never to be
used for the post-shortlist KYC (DEC-008: that attendance is always manual).

Run on the host PC (drives a real browser):
    .venv/Scripts/python -m pia_worker.teams.listener "<meeting link>"

Fragility is acknowledged and logged: Teams DOM changes break selectors; every
step is visible in the logs and the Telegram relay is best-effort, never fatal.
No audio is captured anywhere — captions are the transcript (owner decision:
lightweight over heavy).
"""

import contextlib
import time
from datetime import datetime
from pathlib import Path

import httpx
import structlog
from playwright.sync_api import sync_playwright

from pia_shared.schemas import MeetingSummary
from pia_worker.ai.provider import NIMProvider, ProviderError
from pia_worker.settings import get_settings

logger = structlog.get_logger()

_STATE_FILE = Path(__file__).resolve().parents[3] / "infrastructure" / "teams_session.json"
_TRANSCRIPT_DIR = Path(__file__).resolve().parents[3] / "transcripts"
_FORM_LINK = (
    "https?://(?:forms\\.(?:office|glide)\\.com|docs\\.google\\.com/forms)"
    "[^\\s\"<>]*"
)
_FORM_RE = None  # compiled lazily inside watch loop
_CHAT_SELECTORS = [
    "[role='list'] [data-tid='chat-pane-item']",  # best-effort; Teams DOM shifts
    "div[data-tid='message-body-content']",
]
_CAPTION_SELECTORS = [
    "span[data-tid='closed-caption-text']",
    "[data-tid='closed-caption'] span",
]


def _relay_form_link(link: str) -> None:
    """Instant Telegram relay — the owner's primary ask (fills the form himself)."""
    settings = get_settings()
    text = (
        "📝 <b>KYC feedback form is up</b>\n"
        f"<a href='{link}'>Fill it now (manually — always yours)</a>\n"
        "The listener will leave the session shortly."
    )
    try:
        httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        logger.info("form_link_relayed", link=link[:60])
    except Exception as exc:  # noqa: BLE001 — relay is best-effort
        logger.warning("form_relay_failed", error=str(exc)[:120])


def _extract_chat_links(page) -> list[str]:
    import re as _re

    global _FORM_RE
    if _FORM_RE is None:
        _FORM_RE = _re.compile(_FORM_LINK)
    links: list[str] = []
    for selector in _CHAT_SELECTORS:
        try:
            for el in page.query_selector_all(selector):
                for a in el.query_selector_all("a[href]"):
                    href = a.get_attribute("href") or ""
                    if _FORM_RE.search(href):
                        links.append(href)
                text = (el.text_content() or "")
                for m in _FORM_RE.finditer(text):
                    links.append(m.group(0))
        except Exception:  # noqa: BLE001 — DOM shifts are expected
            continue
    return list(dict.fromkeys(links))


def _extract_captions(page, seen: set[str]) -> list[str]:
    """New live-caption segments from the captions pane (best-effort DOM)."""
    new: list[str] = []
    for selector in _CAPTION_SELECTORS:
        try:
            for el in page.query_selector_all(selector):
                text = (el.text_content() or "").strip()
                if text and text not in seen:
                    seen.add(text)
                    new.append(text)
        except Exception:  # noqa: BLE001 — DOM shifts are expected
            continue
    return new


def _try_enable_captions(page) -> bool:
    """Attempt to turn on live captions (More options menu). Best-effort."""
    with contextlib.suppress(Exception):
        page.get_by_role("button", name="More", exact=False).first.click(timeout=2000)
        for label in ("Turn on live captions", "Live captions"):
            try:
                page.get_by_text(label, exact=False).first.click(timeout=2000)
                logger.info("captions_enabled")
                return True
            except Exception:  # noqa: BLE001
                continue
    logger.warning("captions_enable_attempted_unclear")
    return False


def _summarize(transcript_text: str, form_link: str | None) -> str:
    """LLM summary of the discussion from the captured captions; deterministic
    fallback returns the raw transcript (source-backed, never invented)."""
    prompt = (
        "This is the live-captions transcript of a company KYC information "
        "session. Summarize for the student: the company, the designation/role "
        "discussed, package/salary if mentioned, and key points. Use only this "
        "text.\n\nTRANSCRIPT:\n" + transcript_text[:12000]
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
        head = transcript_text[:1500]
        tail = f"\n\n📝 Feedback form: {form_link}" if form_link else ""
        return (f"LLM unavailable — raw transcript (first {len(head)} chars):\n"
                f"{head}{tail}")


def listen(meeting_url: str, *, max_minutes: int = 180,
           join_now: bool = True) -> str:
    """Join the meeting, watch the chat for the feedback form, relay, leave."""
    if "teams.microsoft" not in meeting_url and "teams.live" not in meeting_url:
        return "not_a_teams_link"
    if not _STATE_FILE.exists():
        return "login_needed: run pia_worker.teams.login_save first"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # headed: audio keeps playing
        context = browser.new_context(
            storage_state=str(_STATE_FILE), viewport={"width": 1400, "height": 900},
            permissions=["microphone", "camera"],  # pre-join screen toggles handled anyway
        )
        page = context.new_page()
        logger.info("listener_opening_meeting")
        page.goto(meeting_url, timeout=60_000)
        page.wait_for_timeout(6000)

        # Pre-join screen: ensure mic/camera are OFF, then Join.
        for toggle_label in ("camera", "mic", "Caméra", "Mikrofon"):
            try:
                page.get_by_label(toggle_label, exact=False).first.click(timeout=1500)
            except Exception:  # noqa: BLE001
                continue
        joined = False
        for label in ("Join now", "Jetzt beitreten", "Rejoindre"):
            try:
                page.get_by_role("button", name=label).first.click(timeout=3000)
                joined = True
                break
            except Exception:  # noqa: BLE001
                continue
        if not joined:
            logger.warning("listener_join_button_not_found")
            browser.close()
            return "join_failed"
        logger.info("listener_joined")
        _try_enable_captions(page)

        transcript_file = _TRANSCRIPT_DIR / (
            "kyc_" + datetime.now().strftime("%Y%m%d_%H%M") + ".txt")
        transcript_file.parent.mkdir(parents=True, exist_ok=True)
        seen_captions: set[str] = set()
        transcript_handle = transcript_file.open("a", encoding="utf-8")

        relayed: set[str] = set()
        form_link: str | None = None
        deadline = time.time() + max_minutes * 60
        try:
            while time.time() < deadline:
                for segment in _extract_captions(page, seen_captions):
                    transcript_handle.write(segment + "\n")
                    transcript_handle.flush()
                for link in _extract_chat_links(page):
                    if link not in relayed:
                        relayed.add(link)
                        form_link = link
                        _relay_form_link(link)
                if relayed:
                    # form is up: linger 2 minutes so final notes/captions settle,
                    # then leave (DEC-008 amendment flow).
                    logger.info("form_found_leaving_soon")
                    time.sleep(120)
                    break
                time.sleep(5)
        finally:
            transcript_handle.close()
            with contextlib.suppress(Exception):
                page.get_by_role("button", name="Leave").first.click(timeout=3000)
            browser.close()

    transcript_text = transcript_file.read_text(encoding="utf-8").strip()
    if transcript_text:
        summary = _summarize(transcript_text, form_link)
        try:
            settings = get_settings()
            httpx.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": settings.telegram_chat_id, "text": summary,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=15,
            )
            logger.info("session_summary_sent")
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("summary_send_failed", error=str(exc)[:120])
    else:
        summary = "no captions captured"
        logger.info("no_transcript_captured")

    logger.info("listener_done", forms_relayed=len(relayed),
                transcript=str(transcript_file))
    return f"done:{len(relayed)}:form_links:transcript={transcript_file.name}"
