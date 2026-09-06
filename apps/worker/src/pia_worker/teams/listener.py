"""Type-1 KYC personal listener (DEC-008 amendment, owner decision 2026-09-06).

Joins an INFORMATIONAL company-arrival Teams session with the owner's saved
session, watches the meeting chat for the feedback-form link, relays it to
Telegram instantly, and leaves shortly after. Never to be used for the
post-shortlist KYC (DEC-008: that attendance is always manual).

Run on the host PC (drives a real browser):
    .venv/Scripts/python -m pia_worker.teams.listener "<meeting link>" [--at "YYYY-MM-DD HH:MM"]

Fragility is acknowledged and logged: Teams DOM changes break selectors; every
step is visible in the logs and the Telegram relay is best-effort, never fatal.
"""

import contextlib
import time
from pathlib import Path

import httpx
import structlog
from playwright.sync_api import sync_playwright

from pia_worker.settings import get_settings

logger = structlog.get_logger()

_STATE_FILE = Path(__file__).resolve().parents[3] / "infrastructure" / "teams_session.json"
_FORM_LINK = (
    "https?://(?:forms\\.(?:office|glide)\\.com|docs\\.google\\.com/forms)"
    "[^\\s\"<>]*"
)
_FORM_RE = None  # compiled lazily inside watch loop (re2-free stdlib only)
_CHAT_SELECTORS = [
    "[role='list'] [data-tid='chat-pane-item']",  # best-effort; Teams DOM shifts
    "div[data-tid='message-body-content']",
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

        relayed: set[str] = set()
        deadline = time.time() + max_minutes * 60
        try:
            while time.time() < deadline:
                for link in _extract_chat_links(page):
                    if link not in relayed:
                        relayed.add(link)
                        _relay_form_link(link)
                if relayed:
                    # form is up: linger 2 minutes so attendance/final notes settle,
                    # then leave (DEC-008 amendment flow).
                    logger.info("form_found_leaving_soon")
                    time.sleep(120)
                    break
                time.sleep(5)
        finally:
            with contextlib.suppress(Exception):
                page.get_by_role("button", name="Leave").first.click(timeout=3000)
            browser.close()

    logger.info("listener_done", forms_relayed=len(relayed))
    return f"done:{len(relayed)}:form_links"
