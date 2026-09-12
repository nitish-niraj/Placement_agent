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
import re
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

# Login-popup credentials come ONLY from the environment (DEC-008 amendment:
# the listener joins AS the owner). Set TEAMS_EMAIL / TEAMS_PASSWORD in
# infrastructure/.env — never hardcode them here.

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


# Media-toggle state detection (pre-join + meeting bar). The Teams UI labels
# these buttons by CURRENT state — from transcripts/controls_* dumps: "Mute
# mic"/"Turn camera off" render only while the device is LIVE; off-state labels
# are "Unmute mic"/"Turn camera on"/"Camera off". Substring name-matching is
# unsafe ("Unmute mic" contains "mute mic"), hence anchored regexes + the
# live-check winning over the dead-check ("Turn camera off" contains
# "camera off" but means LIVE).
_MEDIA_RE = re.compile(r"camera|video|\bmic\b|microphone", re.IGNORECASE)
_LIVE_RE = re.compile(
    r"turn (the )?(camera|video|mic|microphone) off|\bmute\b"
    r"|\b(mic|camera|video) on\b|with (the )?(camera|mic|microphone) on",
    re.IGNORECASE)
_DEAD_RE = re.compile(
    r"turn (the )?(camera|video|mic|microphone) on|unmute"
    r"|\b(mic|microphone|camera|video)( is)? off\b", re.IGNORECASE)


def _is_login_url(url: str) -> bool:
    """Microsoft login surfaces (work accounts use microsoftonline, personal
    accounts login.live.com) — popup or same-tab alike."""
    return ("login.microsoftonline" in url or "login.live.com" in url
            or "microsoftonline.com" in url or "account.live.com" in url)


def _media_button_should_click(btn) -> bool:  # noqa: ANN001 — playwright handle
    """True when this aria-labelled button toggles a currently-LIVE device."""
    with contextlib.suppress(Exception):
        aria = (btn.get_attribute("aria-label") or "").strip()
        if not aria or not _MEDIA_RE.search(aria):
            return False
        if _LIVE_RE.search(aria):
            return True
        if _DEAD_RE.search(aria):
            return False
        pressed = (btn.get_attribute("aria-pressed") or "").lower()
        if pressed in ("true", "false"):
            return pressed == "true"
        # No state signal at all: only a bare toggle label ("Camera",
        # "Microphone", "Mic") justifies clicking on faith.
        return bool(re.match(r"(camera|microphone|mic)\b", aria, re.IGNORECASE))
    return False


def _prejoin_state(pg) -> str:  # noqa: ANN001 — playwright sync Page
    """One-line DOM truth for a pre-join page. Body-text dumps mislead (they
    include hidden nodes); this reports only what the join loop acts on.
    Anchored on Teams' STABLE data-tids (live DOM probe 2026-09-12:
    prejoin-display-name-input / prejoin-join-button / auth-sign-in-link /
    calling-lobby-screen), legacy selectors as fallback."""
    parts: list[str] = [f"url={pg.url[:70]}"]
    with contextlib.suppress(Exception):
        box = pg.locator("[data-tid='prejoin-display-name-input'],"
                         " input[placeholder*='name' i]").first
        if box.count() > 0 and box.is_visible():
            parts.append(f"name={box.input_value()[:20]!r}")
        else:
            parts.append("name=none")
    with contextlib.suppress(Exception):
        jn = pg.locator("[data-tid='prejoin-join-button']").first
        if jn.count() == 0:
            jn = pg.get_by_role("button", name="Join now",
                                exact=False).first
        vis = jn.count() > 0 and jn.is_visible()
        parts.append(
            f"joinnow={'on' if vis and jn.is_enabled(timeout=400) else 'off'}")
    with contextlib.suppress(Exception):
        si = pg.locator("[data-tid='auth-sign-in-link']").first
        parts.append(
            f"signin={'y' if si.count() > 0 and si.is_visible() else 'n'}")
    with contextlib.suppress(Exception):
        pc = pg.get_by_text("Privacy and cookies", exact=False).first
        if pc.count() > 0 and pc.is_visible():
            parts.append("consent=y")
    with contextlib.suppress(Exception):
        if pg.locator("[data-tid='calling-lobby-screen']").count() > 0:
            parts.append("lobby=y")
    with contextlib.suppress(Exception):
        lf = sum(1 for fr in pg.frames if _is_login_url(fr.url))
        if lf:
            parts.append(f"login_frames={lf}")
    return " ".join(parts)


def _prejoin_status(pg) -> str:  # noqa: ANN001
    """Normalized visible text of the light-meetings pre-join screen. Carries
    the device-state phrases the toggles flip ('With camera on/off', 'Mic
    on/off') — the only state signal, since the toggles themselves are
    unlabeled. Empty for other pages."""
    with contextlib.suppress(Exception):
        t = pg.text_content("[data-tid='calling-prejoin-screen']") or ""
        return " ".join(t.split()).lower()
    return ""


def _click_signin(pg) -> bool:  # noqa: ANN001
    """Click the guest pre-join's Sign-in control: data-tid first
    ('auth-sign-in-link', proven live), then role-/text-EXACT fallbacks.
    NOT get_by_text(exact=False) — its container matches center-click the
    wrong element (the bug behind every silent stall since 2026-09-09)."""
    with contextlib.suppress(Exception):
        link = pg.locator("[data-tid='auth-sign-in-link']").first
        if link.count() > 0 and link.is_visible():
            link.click(timeout=2500)
            logger.info("prejoin_signin_clicked", via="tid")
            return True
    for kind in ("link", "button"):
        with contextlib.suppress(Exception):
            sign = pg.get_by_role(kind, name="Sign in", exact=True).first
            if sign.count() > 0 and sign.is_visible(timeout=600):
                sign.click(timeout=2500)
                logger.info("prejoin_signin_clicked", via=kind)
                return True
    with contextlib.suppress(Exception):
        sign = pg.get_by_text("Sign in", exact=True).first
        if sign.count() > 0 and sign.is_visible(timeout=600):
            sign.click(timeout=2500)
            logger.info("prejoin_signin_clicked", via="text-exact")
            return True
    return False


def _dismiss_consent(pg, steps: dict[int, int]) -> str:  # noqa: ANN001
    """The pre-join 'Sign in' click first surfaces a Microsoft 'Privacy and
    cookies' NOTICE FLYOUT that overlays the pre-join and silently swallows
    every later click — including Join now's. Its buttons are only Close +
    'Next' (informational carousel) + a privacy-statement link — probe 4
    proof, 2026-09-12 — so the play is: accept if an accept button ever
    appears, step Next ONCE, otherwise CLOSE it (a stuck-open flyout blocked
    every join on 2026-09-09/12; closing is what unblocked run 5's join).

    Matching is by EQUAL textContent of real <button> elements — the flyout
    buttons carry NO role/aria attrs, and substring matching is dangerous:
    has-text('OK') matches 'Privacy and cookies' and pops the statement."""
    try:
        head = pg.get_by_text("Privacy and cookies", exact=False).first
        if head.count() == 0 or not head.is_visible():
            return ""
    except Exception:  # noqa: BLE001 — flyout gone / DOM shifted
        return ""
    accept_words = {"accept all", "accept", "allow all", "i agree",
                    "confirm choices", "confirm my choices", "got it",
                    "yes, accept all"}
    accept_btn = next_btn = close_btn = None
    buttons: list = []
    with contextlib.suppress(Exception):
        buttons = pg.query_selector_all("button")
    for btn in buttons:
        with contextlib.suppress(Exception):
            if not btn.is_visible():
                continue
            txt = (btn.text_content() or "").strip().lower()
            if txt in accept_words and accept_btn is None:
                accept_btn = btn
            elif txt == "next" and next_btn is None:
                next_btn = btn
            elif (txt == "close"
                  or "close" in (btn.get_attribute("aria-label") or "")
                  .lower()) and close_btn is None:
                close_btn = btn
    if accept_btn is not None:
        with contextlib.suppress(Exception):
            accept_btn.click(timeout=2000)
        logger.info("consent_accepted")
        return "accepted"
    if next_btn is not None and steps.get(id(pg), 0) < 1:
        steps[id(pg)] = 1
        with contextlib.suppress(Exception):
            next_btn.click(timeout=2000)
        logger.info("consent_stepped")
        return "stepped"
    if close_btn is not None:
        with contextlib.suppress(Exception):
            close_btn.click(timeout=2000)
        logger.info("consent_flyout_closed")
        return "closed"
    with contextlib.suppress(Exception):
        pg.keyboard.press("Escape")
    return "closed"


def _turn_media_off_prejoin(pg, tries: dict[int, int]) -> None:  # noqa: ANN001
    """Light-meetings pre-join mic/camera toggles are UNLABELED icon buttons
    (aria-label/text/data-tid all null — live probe 2026-09-12). Target them
    by stable data-tid neighbours and VERIFY the screen state text flipped
    ('With camera on'->'With camera off', 'Mic on'->'Mic off'); a click that
    does not produce the expected flip is REVERTED, so an errant click can
    never leave a device ON."""
    if tries.get(id(pg), 0) >= 4:
        return
    status = _prejoin_status(pg)
    if not status:
        return
    tries[id(pg)] = tries.get(id(pg), 0) + 1
    pending = 0
    for dev, sel, on_w, off_w in (
        ("camera", "button:has(+ [data-tid='video-flyout-open-button'])",
         "with camera on", "with camera off"),
        ("mic",
         "xpath=//*[@data-tid='selected-microphone-display']"
         "/preceding::button[1]", "mic on", "mic off"),
    ):
        if off_w in status:
            continue  # confirmed off
        if on_w not in status:
            pending += 1
            continue  # no state text yet — retry next cycle
        pending += 1
        with contextlib.suppress(Exception):
            btn = pg.locator(sel).first
            if btn.count() == 0:
                logger.warning("prejoin_media_control_missing", device=dev)
                continue
            btn.click(timeout=2500)
            pg.wait_for_timeout(1200)
            after = _prejoin_status(pg)
            if off_w in after or on_w not in after:
                logger.info("prejoin_media_off", device=dev)
            else:  # wrong element — undo, never join with it flipped ON
                logger.warning("prejoin_media_click_reverted", device=dev)
                with contextlib.suppress(Exception):
                    btn.click(timeout=2500)
    if pending == 0:
        tries[id(pg)] = 99  # both devices confirmed off


def _mute_if_live(page) -> bool:
    """Post-join safety net. Meeting-bar mic/camera buttons carry the
    live-state labels 'Mute mic' / 'Turn camera off' (transcripts/
    controls_menu_* proves they only render while live). Scan every
    aria-labelled button and click the live ones — exact aria-label logic
    (role+name substring matching would also hit 'Unmute mic')."""
    muted = False
    with contextlib.suppress(Exception):
        for btn in page.query_selector_all("button[aria-label]"):
            with contextlib.suppress(Exception):
                if (btn.is_visible() and btn.is_enabled()
                        and _media_button_should_click(btn)):
                    label = (btn.get_attribute("aria-label") or "").strip()
                    btn.click(timeout=2000)
                    muted = True
                    logger.info("media_control_off", control=label[:40])
    return muted


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
    """Turn on live captions. The LPU tenant's meeting bar (from the control
    dumps) exposes a 'More' button whose flyout contains a 'Captions' item —
    click it with text-scoping (role-based lookups miss it: it renders as a
    menuitem/div, not a button)."""
    # strategy 1: direct captions control on the page (any role)
    for label in ("Turn on live captions", "Turn on captions",
                  "Live captions", "Captions"):
        with contextlib.suppress(Exception):
            page.get_by_text(label, exact=False).first.click(timeout=1500)
            logger.info("captions_enabled", via="direct", label=label)
            page.wait_for_timeout(1500)
            return True
    # strategy 2: open the 'More' flyout and click the Captions item inside it
    with contextlib.suppress(Exception):
        page.get_by_role("button", name="More", exact=False).first.click(
            timeout=2500)
        page.wait_for_timeout(1200)  # flyout animation
        _dump_controls(page, "menu")
        for label in ("Captions", "Turn on live captions", "Live captions"):
            with contextlib.suppress(Exception):
                page.get_by_text(label, exact=False).first.click(timeout=2500)
                logger.info("captions_enabled", via="more-flyout", label=label)
                page.wait_for_timeout(1500)
                return True
        with contextlib.suppress(Exception):
            page.keyboard.press("Escape")
    logger.warning("captions_enable_failed")
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

        # Join state machine: poll EVERY open page — the pre-join flow differs
        # by link type: work links go straight to toggles+Join; personal links
        # (teams.live) use the anonymous guest flow, where a NAME field appears
        # first and "Join now" enables only once filled.
        #
        # The listener joins AS the authenticated owner (never "(Unverified)"):
        # right after the guest name it clicks the pre-join's single 'Sign in'
        # control. The Microsoft login then appears EITHER as a new popup
        # (context.on("page") is registered BEFORE that click, so popups are
        # captured from birth) OR as a same-tab navigation. Any page whose URL
        # is a Microsoft login domain is driven email -> password -> Yes/Next
        # (creds from .env; the saved session may also auto-complete it).
        # The authenticated pre-join has NO name field — "Join now" showing
        # without one IS the sign-in success signal; while a login page exists,
        # a missing "Join now" means WAIT, not fail.
        settings = get_settings()

        def _on_new_page(pg) -> None:  # playwright sync Page
            with contextlib.suppress(Exception):
                logger.info("new_page_opened", url=pg.url[:80])

        context.on("page", _on_new_page)

        def _drive_login_page(pg) -> None:  # playwright sync Page
            """Complete a Microsoft login form (popup or same-tab hop)."""
            with contextlib.suppress(Exception):
                email_box = pg.locator(
                    "input[type=email], input[name=loginfmt]").first
                if (email_box.count() > 0 and email_box.is_visible()
                        and settings.teams_email):
                    email_box.fill(settings.teams_email)
                    pg.locator("#idSIButton9, input[type=submit], "
                               "button[type=submit]").first.click(timeout=2000)
                    logger.info("login_email_submitted")
                    time.sleep(2)
            with contextlib.suppress(Exception):
                pw_box = pg.locator("input[type=password]").first
                if (pw_box.count() > 0 and pw_box.is_visible()
                        and settings.teams_password):
                    pw_box.fill(settings.teams_password)
                    pg.locator("#idSIButton9, input[type=submit], "
                               "button[type=submit]").first.click(timeout=2000)
                    logger.info("login_password_submitted")
                    time.sleep(2)
            for label in ("Yes", "Next", "Accept"):
                with contextlib.suppress(Exception):
                    btn = pg.get_by_role("button", name=label, exact=True).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click(timeout=1500)
                        logger.info("login_prompt_answered", label=label)

        joined = False
        joined_page = page
        name_filled_pages: set[int] = set()
        media_off_tries: dict[int, int] = {}
        consent_steps: dict[int, int] = {}
        page_states: dict[int, str] = {}
        signin_attempts = 0
        signin_last_click = 0.0
        signin_pending_until = 0.0
        join_deadline = time.time() + 180  # consent + login hops need patience
        while time.time() < join_deadline and not joined:
            # Microsoft login surfaces first — popup, same-tab navigation, or
            # an embedded login IFRAME (light-meetings host guests' sign-in
            # in one; the page URL never leaves teams.live.com).
            login_surface = False
            for login_pg in list(context.pages):
                if _is_login_url(login_pg.url):
                    login_surface = True
                    _drive_login_page(login_pg)
                    continue
                with contextlib.suppress(Exception):
                    for fr in login_pg.frames:
                        if _is_login_url(fr.url):
                            login_surface = True
                            _drive_login_page(fr)
            for candidate in context.pages:
                if ("/dl/launcher" in candidate.url
                        or _is_login_url(candidate.url)):
                    continue  # chooser / login surface — nothing to join here
                # DOM truth every state change — the silent 150 s stalls of
                # the 2026-09-12 runs were undiagnosable without it.
                state = _prejoin_state(candidate)
                if page_states.get(id(candidate)) != state:
                    page_states[id(candidate)] = state
                    logger.info("prejoin_state", state=state)
                # Consent flyout blocks ALL clicks underneath it — clear it
                # first, every cycle (accept preferred; see helper).
                consent = _dismiss_consent(candidate, consent_steps)
                # mic/camera OFF before joining (owner: the listener never
                # broadcasts) — tid-anchored, text-verified, self-reverting.
                _turn_media_off_prejoin(candidate, media_off_tries)
                guest_screen = "signin=y" in state
                # The hop stalled on a consent screen (accepted just now) or
                # the click was a no-op (probe v2: on anon light meetings
                # 'Sign in' can be inert) — retry the link up to 3x,
                # extending the window; the guest path takes over when it
                # expires.
                retry_signin = (id(candidate) in name_filled_pages
                                and guest_screen
                                and time.time() < signin_pending_until
                                and not login_surface
                                and signin_attempts < 4
                                and (consent == "accepted"
                                     or time.time() - signin_last_click > 15))
                # Guest flow: fill the name ONCE (tid anchor), then attempt
                # the authenticated upgrade via the Sign-in link.
                if id(candidate) not in name_filled_pages:
                    for name_sel in ("[data-tid='prejoin-display-name-input']",
                                     "input[placeholder*='name' i]",
                                     "input[aria-label*='name' i]",
                                     "input[type='text']"):
                        try:
                            box = candidate.locator(name_sel).first
                            if box.count() > 0 and box.is_visible(timeout=500):
                                box.fill("Nitish Kumar")
                                name_filled_pages.add(id(candidate))
                                logger.info("guest_name_filled",
                                            page=candidate.url[:60])
                                if _click_signin(candidate):
                                    signin_attempts = 1
                                    signin_last_click = time.time()
                                    signin_pending_until = time.time() + 90
                                break
                        except Exception:  # noqa: BLE001
                            continue
                elif retry_signin and _click_signin(candidate):
                    signin_attempts += 1
                    signin_last_click = time.time()
                    signin_pending_until = max(
                        signin_pending_until, time.time() + 30)
                # Join decision. While an auth hop is pending on a GUEST
                # screen, HOLD — clicking Join there is what joined run 4 as
                # "(Unverified)". Join when the screen became authenticated
                # (no name field, no Sign-in link), when there was no
                # Sign-in control at all, or when the window expired.
                if login_surface:
                    continue
                hold_guest = (time.time() < signin_pending_until
                              and guest_screen)
                if not hold_guest:
                    for label in ("Join now", "Jetzt beitreten",
                                  "Rejoindre"):
                        try:
                            btn = candidate.locator(
                                "[data-tid='prejoin-join-button']").first
                            if btn.count() == 0:
                                btn = candidate.get_by_role(
                                    "button", name=label,
                                    exact=False).first
                            if btn.is_enabled(timeout=800):
                                btn.click(timeout=3000)
                                joined = True
                                joined_page = candidate
                                logger.info(
                                    "joined_as",
                                    identity="authenticated"
                                    if "signin=n" in state and "name=none"
                                    in state else "guest")
                                break
                        except Exception as exc:  # noqa: BLE001
                            # An ENABLED button whose click times out =
                            # something overlays it (run 3: consent flyout).
                            logger.info("join_now_click_failed",
                                        error=str(exc).splitlines()[0][:140])
                            break
                if joined:
                    break
            if not joined:
                time.sleep(2)
        page = joined_page
        if not joined:
            # Diagnostics: DOM truth FIRST, screenshot LAST — in the 18:43 run
            # a throwing screenshot call inside the shared suppress block
            # killed the whole dump. Each piece now fails independently.
            diag = _TRANSCRIPT_DIR / (
                "join_failed_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
            diag.parent.mkdir(parents=True, exist_ok=True)
            lines = [f"pages: {len(context.pages)}"]
            with contextlib.suppress(Exception):
                lines.append(f"url: {page.url}")
                lines.append(
                    f"body: {(page.text_content('body') or '')[:300]}")
            for pg in context.pages:
                with contextlib.suppress(Exception):
                    lines.append(f"state: {_prejoin_state(pg)}")
                    lines.append("  visible: " + " | ".join(
                        _dump_controls(pg, "failed")[:40]))
            with contextlib.suppress(Exception):
                (diag.with_suffix(".txt")).write_text(
                    "\n".join(lines), encoding="utf-8")
            with contextlib.suppress(Exception):
                page.screenshot(path=str(diag) + ".png", timeout=15000)
            logger.warning("listener_join_button_not_found", diag=str(diag))
            browser.close()
            return "join_failed"
        logger.info("listener_joined")
        # A guest join can stall in the LOBBY (calling-lobby-screen — proven
        # live 2026-09-12) until the host admits. Surface it and wait, so a
        # silent 'joined' never reads as a working listener.
        with contextlib.suppress(Exception):
            if page.locator("[data-tid='calling-lobby-screen']").count() > 0:
                logger.warning("listener_in_lobby",
                               hint="waiting for host to admit")
                lobby_end = time.time() + 120
                while time.time() < lobby_end and page.locator(
                        "[data-tid='calling-lobby-screen']").count() > 0:
                    page.wait_for_timeout(3000)
                logger.info("lobby_wait_over",
                            still_waiting=page.locator(
                                "[data-tid='calling-lobby-screen']").count()
                            > 0)
        _dump_controls(page, "joined")  # evidence for captions/bar selectors
        _open_chat_panel(page)      # form links land in the meeting chat
        _try_enable_captions(page)

        # In-meeting safety: the pre-join toggles sometimes miss (config, race)
        # — if media is still ON after joining, mute mic + turn camera off NOW.
        page.wait_for_timeout(2500)  # let the meeting bar render
        joined_with_media_on = _mute_if_live(page)
        if joined_with_media_on:
            logger.info("media_muted_after_join")

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
                    # form is up: stay 2 more minutes (final announcements and
                    # the last captions land here), then leave automatically
                    # (DEC-008 amendment flow — owner asked for this explicitly).
                    leave_at = time.time() + 120
                    logger.info("form_found_leaving_in_2_minutes")
                    while time.time() < leave_at:
                        for segment in _extract_captions(page, seen_captions):
                            transcript_handle.write(segment + "\n")
                            transcript_handle.flush()
                        time.sleep(5)
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
