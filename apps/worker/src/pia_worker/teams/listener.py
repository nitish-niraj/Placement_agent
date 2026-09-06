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
from pia_worker.teams import is_teams_url

logger = structlog.get_logger()

_STATE_FILE = Path(__file__).resolve().parents[5] / "infrastructure" / "teams_session.json"
_TRANSCRIPT_DIR = Path(__file__).resolve().parents[5] / "transcripts"
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
    # Page-wide anchor scan: form links are caught wherever they render —
    # meeting chat pane, link cards, captions, anywhere (light-meetings UI
    # uses different chat DOM than the main app, so pane targeting is fragile).
    try:
        for a in page.query_selector_all("a[href]"):
            href = a.get_attribute("href") or ""
            if _FORM_RE.search(href):
                links.append(href)
    except Exception:  # noqa: BLE001 — DOM shifts are expected
        pass
    # Text-based scan for links pasted as plain text inside chat messages.
    for selector in _CHAT_SELECTORS:
        try:
            for el in page.query_selector_all(selector):
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


def _open_chat_panel(page) -> None:
    """Open the meeting Chat panel (best-effort) — in the light-meetings UI
    the chat stays collapsed until clicked, and an unrendered pane hides
    messages from the DOM."""
    with contextlib.suppress(Exception):
        page.get_by_role("button", name="Chat", exact=False).first.click(timeout=2500)
        logger.info("chat_panel_opened")
        page.wait_for_timeout(1500)


def _dump_controls(page, tag: str) -> list[str]:
    """Diagnostic: every visible button/menuitem label — saved to the transcripts
    dir so the exact control names are known for precise clicking."""
    labels: list[str] = []
    try:
        elements = page.query_selector_all("button, [role=menuitem], [role=button]")
        for el in elements:
            with contextlib.suppress(Exception):
                if not el.is_visible():
                    continue
                label = ((el.get_attribute("aria-label")
                          or el.text_content() or "").strip())
                if label and len(label) < 60:
                    labels.append(label)
    except Exception:  # noqa: BLE001 — diagnostics are best-effort
        pass
    if labels:
        dump = _TRANSCRIPT_DIR / f"controls_{tag}_{datetime.now().strftime('%H%M%S')}.txt"
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text("\n".join(dict.fromkeys(labels)), encoding="utf-8")
    return labels


def _try_enable_captions(page) -> bool:
    """Turn on live captions. Two strategies, diagnostics in between:
    1. direct CC/captions control anywhere on the page (aria-label or text);
    2. open More ('…') menus and look inside them.
    All visible controls are dumped first so failures are debuggable."""
    controls = _dump_controls(page, "joined")
    # strategy 1: a direct captions control on the page
    for label in ("Turn on live captions", "Turn on captions", "Live captions",
                  "Captions", "CC"):
        with contextlib.suppress(Exception):
            btn = page.get_by_role("button", name=label, exact=False).first
            btn.click(timeout=1500)
            logger.info("captions_enabled", via="direct", label=label)
            page.wait_for_timeout(1500)
            return True
    # strategy 2: open every 'More'-ish menu and search inside
    for more_label in ("More options", "More", "More actions"):
        with contextlib.suppress(Exception):
            page.get_by_role("button", name=more_label, exact=False).first.click(
                timeout=2000)
            page.wait_for_timeout(1000)
            _dump_controls(page, "menu")
            for label in ("Turn on live captions", "Turn on captions",
                          "Live captions", "Captions"):
                with contextlib.suppress(Exception):
                    page.get_by_text(label, exact=False).first.click(timeout=2000)
                    logger.info("captions_enabled", via="menu", label=label)
                    page.wait_for_timeout(1500)
                    return True
            with contextlib.suppress(Exception):
                page.keyboard.press("Escape")
    logger.warning("captions_enable_failed", visible_controls=len(controls))
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


def _resolve_link(url: str) -> str:
    """Follow URL shorteners (tinyurl etc.) to the real Teams meeting URL."""
    try:
        response = httpx.get(url, follow_redirects=True, timeout=20)
        resolved = str(response.url)
        if resolved != url:
            logger.info("short_link_resolved", final=resolved[:80])
        return resolved
    except httpx.HTTPError as exc:
        logger.warning("short_link_resolve_failed", error=str(exc)[:120])
        return url  # the browser may still follow it


def _direct_meeting_url(launcher_url: str) -> str:
    """Skip the launcher entirely: the /dl/launcher page encodes the real
    meeting URL in its ?url= parameter ("/_#/meet/<id>?p=<passcode>"). Navigating
    there directly avoids the launcher's ms-teams: app deep-link, whose native
    Chromium dialog ("Open URL:ms-teams?") blocks automation. Anonymous join is
    requested for personal links (the guest name is filled in the pre-join)."""
    from urllib.parse import parse_qs, unquote, urlparse

    parsed = urlparse(launcher_url)
    inner = parse_qs(parsed.query).get("url", [None])[0]
    if not inner:
        return launcher_url
    decoded = unquote(inner)
    if not decoded.startswith("/"):
        return launcher_url
    direct = f"{parsed.scheme}://{parsed.netloc}{decoded}"
    if "anon=" not in decoded and "teams.live" in parsed.netloc:
        direct += "&anon=true"
    logger.info("direct_meeting_url", url=direct[:90])
    return direct


def listen(meeting_url: str, *, max_minutes: int = 180,
           join_now: bool = True) -> str:
    """Join the meeting, watch the chat for the feedback form, relay, leave."""
    meeting_url = _direct_meeting_url(_resolve_link(meeting_url))
    if not is_teams_url(meeting_url):
        return "not_a_teams_link"
    if not _STATE_FILE.exists():
        return "login_needed: run pia_worker.teams.login_save first"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # headed: audio keeps playing
        context = browser.new_context(
            storage_state=str(_STATE_FILE), viewport={"width": 1400, "height": 900},
            permissions=["microphone", "camera"],  # pre-join screen toggles handled anyway
            # Realistic UA: Teams serves the full app shell to real browsers;
            # the default automation UA gets a degraded, never-loading shell.
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"),
            locale="en-IN",
        )
        page = context.new_page()

        # Warm-up: the clean app entry loads the authenticated SPA first.
        # Meeting links opened cold hang on the migration/policy redirect.
        logger.info("listener_warming_up_app_shell")
        page.goto("https://teams.cloud.microsoft/", timeout=60_000)
        page.wait_for_timeout(15_000)

        logger.info("listener_opening_meeting")
        page.goto(meeting_url, timeout=60_000)
        page.wait_for_timeout(8000)

        # Shortener/deep links land on a launcher: "Join your Teams meeting —
        # Continue on this browser | Join on the Teams app". The click may open
        # the meeting in-page OR in a new tab — both are handled by iterating
        # every open page in the join state machine below.
        for label in ("Continue on this browser",
                      "Continue on this device", "Fortfahren im Browser"):
            try:
                page.get_by_text(label, exact=False).first.click(timeout=3000)
                logger.info("launcher_continue_clicked", label=label)
                page.wait_for_timeout(5000)
                break
            except Exception:  # noqa: BLE001 — direct links skip the launcher
                continue

        # Join state machine (90s): poll EVERY open page — the pre-join flow
        # differs by link type: work links go straight to toggles+Join;
        # personal links (teams.live) use the anonymous guest flow, where a
        # NAME field appears first and "Join now" enables only once filled.
        joined = False
        joined_page = page
        name_filled_pages: set[int] = set()
        join_deadline = time.time() + 90
        while time.time() < join_deadline and joined_page is page and not joined:
            for candidate in context.pages:
                if "/dl/launcher" in candidate.url:
                    continue  # the chooser itself — nothing to join here
                # mic/camera off whenever the toggles are present
                for toggle_label in ("camera", "mic", "Caméra", "Mikrofon"):
                    with contextlib.suppress(Exception):
                        candidate.get_by_label(toggle_label, exact=False).first.click(
                            timeout=800)
                # guest name entry (personal links)
                if id(candidate) not in name_filled_pages:
                    for name_sel in ("input[placeholder*='name' i]",
                                     "input[aria-label*='name' i]",
                                     "input[type='text']"):
                        try:
                            box = candidate.locator(name_sel).first
                            if box.is_visible(timeout=500):
                                box.fill("Nitish Kumar")
                                name_filled_pages.add(id(candidate))
                                logger.info("guest_name_filled",
                                            page=candidate.url[:60])
                                break
                        except Exception:  # noqa: BLE001
                            continue
                # Join button (enabled once a name is set / no name required)
                for label in ("Join now", "Jetzt beitreten", "Rejoindre"):
                    try:
                        btn = candidate.get_by_role("button", name=label).first
                        if btn.is_enabled(timeout=800):
                            btn.click(timeout=3000)
                            joined = True
                            joined_page = candidate
                            break
                    except Exception:  # noqa: BLE001
                        continue
                if joined:
                    break
            if not joined:
                time.sleep(2)
        page = joined_page
        if not joined:
            # Diagnostics: screenshot + visible text so join failures are
            # always explainable (ended meeting, lobby, permission wall…).
            diag = _TRANSCRIPT_DIR / (
                "join_failed_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
            diag.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(Exception):
                page.screenshot(path=str(diag) + ".png")
                body = (page.text_content("body") or "").replace("\n", " ")[:300]
                (diag.with_suffix(".txt")).write_text(
                    f"url: {page.url}\ntitle: {page.title()}\nbody: {body}",
                    encoding="utf-8")
            logger.warning("listener_join_button_not_found", diag=str(diag))
            browser.close()
            return "join_failed"
        logger.info("listener_joined")
        _open_chat_panel(page)      # form links land in the meeting chat
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


def main() -> None:
    """CLI: .venv/Scripts/python -m pia_worker.teams.listener "<meeting link>"
    [--minutes N] — joins, watches chat for the feedback form, leaves."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Type-1 KYC personal listener (DEC-008 amendment)")
    parser.add_argument("url", help="Teams meeting link (any domain/shortener)")
    parser.add_argument("--minutes", type=int, default=180,
                        help="max minutes to stay in the meeting")
    args = parser.parse_args()
    result = listen(args.url, max_minutes=args.minutes)
    print(result)


if __name__ == "__main__":
    main()
