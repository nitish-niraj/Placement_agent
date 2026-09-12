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


def _safe_wait(pg, ms: int) -> None:  # noqa: ANN001
    """wait_for_timeout that survives a closed tab — run 11 died on the
    first unguarded call after Teams replaced the join tab post-auth."""
    with contextlib.suppress(Exception):
        if not pg.is_closed():
            pg.wait_for_timeout(ms)


def _resolve_live_page(context, old):  # noqa: ANN001
    """Find the page that actually hosts the meeting: the auth redirect can
    CLOSE the tab whose Join button we clicked (run 11 crash). A meeting tab
    shows 'Leave' or the lobby screen; fall back to any live tab."""
    for _ in range(20):
        for pg in context.pages:
            with contextlib.suppress(Exception):
                if pg.is_closed():
                    continue
                if (pg.locator("button:has-text('Leave')").count() > 0
                        or pg.locator(
                            "[data-tid='calling-lobby-screen']").count() > 0):
                    return pg
        with contextlib.suppress(Exception):
            if not old.is_closed() and ("v2/" in old.url
                                        or "/meet" in old.url):
                return old
        time.sleep(1)
    for pg in context.pages:
        with contextlib.suppress(Exception):
            if not pg.is_closed():
                return pg
    return old


def _telegram_send(*, text: str | None = None, photo: bytes | None = None,
                   caption: str | None = None) -> bool:
    """One Telegram door: sendMessage or sendPhoto. Best-effort, never fatal."""
    settings = get_settings()
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    try:
        if photo is not None:
            httpx.post(
                f"{base}/sendPhoto",
                data={"chat_id": settings.telegram_chat_id,
                      "caption": caption or ""},
                files={"photo": photo}, timeout=30)
        else:
            httpx.post(
                f"{base}/sendMessage",
                data={"chat_id": settings.telegram_chat_id,
                      "text": text or "", "parse_mode": "HTML",
                      "disable_web_page_preview": "true"},
                timeout=20)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_send_failed", error=str(exc)[:120])
        return False


def _send_join_proof(pg, identity: str) -> None:  # noqa: ANN001
    """Owner's ask: a screenshot on Telegram proving the join happened."""
    shot = _TRANSCRIPT_DIR / ("join_proof_"
                              + datetime.now().strftime("%Y%m%d_%H%M%S")
                              + ".png")
    shot.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(Exception):
        pg.screenshot(path=str(shot), timeout=15000)
        _telegram_send(
            photo=shot.read_bytes(),
            caption=(f"✅ PIA joined the Teams session (identity: "
                     f"{identity}, LPU account) at "
                     + datetime.now().strftime("%H:%M:%S")
                     + " — mic/camera verified off, captions state shown "
                       "in-meeting."))


# Self-introductions in captions/chat ('this is X', "I'm X from Y") — the
# presenter/teacher name the owner wants alongside the form-link relay.
_SELF_INTRO = re.compile(
    r"\b(?:i am|i'm|this is|my name is|name is|name's|here's|"
    r"joining us(?:\s+today)? is|with (?:me|us)(?:\s+today)? is)\s+"
    r"((?:[A-Z][A-Za-z. '-]{1,20}\s){0,3}[A-Z][A-Za-z. '-]{1,20})")
_NOT_NAMES = {"Microsoft", "Sorry", "Hello", "Hi", "Thanks", "Okay", "Yes",
              "Team", "Everyone", "Someone", "Back", "Here"}


def _presenter_names(caption_text: str) -> list[str]:
    """Best-guess names of whoever led the session, most-mentioned first."""
    counts: dict[str, int] = {}
    for m in _SELF_INTRO.finditer(caption_text):
        nm = " ".join(m.group(1).split())[:40]
        if not nm or nm.split()[0] in _NOT_NAMES:
            continue
        counts[nm] = counts.get(nm, 0) + 1
    return [n for n, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:3]


def _relay_form_link(link: str, presenters: tuple[str, ...] = ()) -> None:
    """Instant Telegram relay — the owner's primary ask (fills the form
    himself), plus the teacher/presenter name if the session revealed one."""
    who = (", ".join(presenters) if presenters
           else "not detected in captions/chat yet")
    text = (
        "📝 <b>KYC feedback form is up</b>\n"
        f"<a href='{link}'>Fill it now (manually — always yours)</a>\n"
        f"👤 Teacher/presenter: {who}\n"
        "The listener will leave the session shortly.")
    if _telegram_send(text=text):
        logger.info("form_link_relayed", link=link[:60], presenters=who)


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


# Media-toggle state detection (pre-join + meeting bar). Teams labels these
# buttons by CURRENT state: 'Mute mic'/'Turn camera off' render only while
# LIVE; 'Unmute mic'/'Turn camera on' mean the device is OFF. ACTION phrases
# take precedence over STATE phrases (run 9 bug: 'turn camera on' contains
# the state phrase 'camera on' — priority matching turned the camera ON).
_MEDIA_RE = re.compile(r"camera|video|\bmic\b|microphone", re.IGNORECASE)
_LIVE_ACTION_RE = re.compile(
    r"turn (the )?(camera|video|mic|microphone) off|\bmute\b", re.IGNORECASE)
_DEAD_ACTION_RE = re.compile(
    r"turn (the )?(camera|video|mic|microphone) on|unmute", re.IGNORECASE)
_LIVE_STATE_RE = re.compile(
    r"\b(mic|camera|video)( is)? on\b|with (the )?(camera|mic|microphone) on",
    re.IGNORECASE)
_DEAD_STATE_RE = re.compile(
    r"\b(mic|microphone|camera|video)( is)? off\b", re.IGNORECASE)


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
        if _LIVE_ACTION_RE.search(aria):
            return True
        if _DEAD_ACTION_RE.search(aria):
            return False
        if _LIVE_STATE_RE.search(aria):
            return True
        if _DEAD_STATE_RE.search(aria):
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
            # ...but only OUTSIDE the login dialog — probe 5: that dialog's
            # footer carries the same 'Privacy and cookies' text.
            try:
                in_dialog = pg.locator("[role='dialog'] input").count() > 0
            except Exception:  # noqa: BLE001
                in_dialog = False
            parts.append("dialog=y" if in_dialog else "consent=y")
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


def _drive_signin_dialog(pg, settings) -> bool:  # noqa: ANN001
    """Advance the EMBEDDED Microsoft sign-in dialog the pre-join 'Sign in'
    link opens IN-PAGE (probe 5, 2026-09-12: role=dialog, 'Enter your email
    or phone number' + Next; no popup, no iframe, no login URL — which is
    why every login-surface detector missed it and auth never happened).
    CASES, each detected from the dialog's own content, per cycle:
      1. 'Enter your email or phone number'  -> fill TEAMS_EMAIL, Next
      2. 'Pick an account' (cached list)     -> click the LPU row
      3. password screen                     -> fill TEAMS_PASSWORD, Next
      4. 'Stay signed in?'                   -> Yes
      5. MFA / 'Let's confirm it's you'      -> leave open, log for phone
      6. credentials error                   -> loud warning (owner fixes
         .env; the guest fallback still joins)
    Returns True while a dialog is being worked on."""
    try:
        dlg = pg.locator("[role='dialog']").first
        if dlg.count() == 0 or not dlg.is_visible():
            return False
        dtxt = (dlg.text_content() or "").lower()
    except Exception:  # noqa: BLE001
        return False
    worked = False
    if any(k in dtxt for k in ("wrong password", "incorrect password",
                               "credentials that don't match",
                               "we couldn't find an account")):
        logger.error("signin_dialog_credentials_error",
                     hint="check TEAMS_EMAIL/TEAMS_PASSWORD in "
                          "infrastructure/.env — joining as guest instead")
        with contextlib.suppress(Exception):
            dlg.get_by_role("button", name="Back",
                            exact=True).first.click(timeout=1500)
        return False
    # Case 3: password (checked first — MS can prefill the email and show it)
    with contextlib.suppress(Exception):
        pw = dlg.locator("input[type=password]").first
        if pw.count() > 0 and pw.is_visible() and settings.teams_password:
            if (pw.input_value() or "") != settings.teams_password:
                pw.fill(settings.teams_password)
                logger.info("signin_dialog_password_filled")
            dlg.get_by_role("button", name=re.compile(
                r"^(next|sign in)$", re.I)).first.click(timeout=2000)
            logger.info("signin_dialog_password_submitted")
            worked = True
    # Case 1: email/phone input
    with contextlib.suppress(Exception):
        em = dlg.locator(
            "input[type=email], input[name=loginfmt], "
            "input[type=tel], input[type=text]").first
        if (not worked and em.count() > 0 and em.is_visible()
                and settings.teams_email):
            if (em.input_value() or "") != settings.teams_email:
                em.fill(settings.teams_email)
                logger.info("signin_dialog_email_filled",
                            account=settings.teams_email)
                dlg.get_by_role("button", name=re.compile(
                    r"^next$", re.I)).first.click(timeout=2000)
            worked = True
    # Case 2: cached-account picker (no input, email rows to click)
    with contextlib.suppress(Exception):
        if (not worked and settings.teams_email
                and ("pick an account" in dtxt
                     or "choose an account" in dtxt
                     or settings.teams_email in dtxt)):
            row = dlg.get_by_text(settings.teams_email,
                                  exact=False).first
            if row.count() > 0 and row.is_visible():
                row.click(timeout=2000)
                logger.info("signin_dialog_account_picked",
                            account=settings.teams_email)
                worked = True
    # Case 4: 'Stay signed in?'
    with contextlib.suppress(Exception):
        yes = dlg.get_by_role("button", name="Yes", exact=True).first
        if not worked and yes.count() > 0 and yes.is_visible():
            yes.click(timeout=2000)
            logger.info("signin_dialog_stay_signed_in_yes")
            worked = True
    # Case 5: MFA — surface it; the owner approves on the phone while the
    # join loop holds the guest join (dialog activity keeps extending it).
    if not worked and any(k in dtxt for k in ("authenticator",
                                              "we sent a code",
                                              "enter the code", "text us",
                                              "confirm it's you",
                                              "let's protect your account")):
        logger.warning("signin_dialog_mfa_pending",
                       hint="approve on the phone / enter the code")
        worked = True
    return worked


def _dismiss_consent(pg, steps: dict[int, int]) -> str:  # noqa: ANN001
    """Close a genuine 'Privacy and cookies' NOTICE flyout if one exists.

    PROBE 5 (2026-09-12) showed the flyout with 'Close | Next | Privacy and
    cookies' that blocked every join is NOT a notice — it is the EMBEDDED
    Microsoft sign-in dialog ("Enter your email or phone number" + Next),
    which _drive_signin_dialog fills. So this only acts when the text appears
    with NO dialog input present (a real notice), and NEVER while the login
    dialog is on screen — closing that was the self-sabotage that kept every
    run on guest identity.
    """
    try:
        head = pg.get_by_text("Privacy and cookies", exact=False).first
        if head.count() == 0 or not head.is_visible():
            return ""
    except Exception:  # noqa: BLE001 — DOM shifted
        return ""
    try:
        if pg.locator("[role='dialog'] input").count() > 0:
            return ""  # live login dialog — NOT consent, hands off
    except Exception:  # noqa: BLE001
        pass
    accept_words = {"accept all", "accept", "allow all", "i agree",
                    "confirm choices", "confirm my choices", "got it"}
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
            elif txt == "close" and close_btn is None:
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
    return ""


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


def _captions_on(pg) -> bool:  # noqa: ANN001
    """Ground truth for captions: the caption pane exists / has content, or a
    control has flipped to the live 'Turn off captions' state."""
    for sel in _CAPTION_SELECTORS:
        with contextlib.suppress(Exception):
            if pg.locator(sel).count() > 0:
                return True
    with contextlib.suppress(Exception):
        if pg.get_by_role("region",
                          name=re.compile(r"caption", re.I)).count() > 0:
            return True
    with contextlib.suppress(Exception):
        if pg.get_by_text("turn off captions",
                          exact=False).first.is_visible():
            return True
    with contextlib.suppress(Exception):
        if pg.get_by_text("turn off live captions",
                          exact=False).first.is_visible():
            return True
    with contextlib.suppress(Exception):
        if pg.get_by_text("hide live captions",
                          exact=False).first.is_visible():
            return True
    return False


def _await_captions(pg, seconds: float = 8.0) -> bool:  # noqa: ANN001
    end = time.time() + seconds
    while time.time() < end:
        if _captions_on(pg):
            logger.info("captions_enabled_verified")
            return True
        with contextlib.suppress(Exception):
            pg.wait_for_timeout(1000)
    return False


def _click_by_text(pg, label: str) -> bool:  # noqa: ANN001
    """Click the most button-like element carrying `label` — tried as exact
    text, accessible NAME (aria-label / nested-span labels — run 10: the More
    flyout's 'Language and speech'/'Record and transcribe' items have no
    clickable exact-text node), and loose text. Visible-only, never raises."""
    rx = re.compile(rf"\b{re.escape(label)}\b", re.I)
    attempts = (
        lambda: pg.get_by_text(label, exact=True),
        lambda: pg.get_by_role("menuitem", name=rx),
        lambda: pg.get_by_role("button", name=rx),
        lambda: pg.locator(f"[aria-label*='{label}' i],[title*='{label}' i]"),
        lambda: pg.get_by_text(label, exact=False),
    )
    for make in attempts:
        with contextlib.suppress(Exception):
            loc = make().last
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=2000)
                logger.info("caption_control_clicked", label=label)
                return True
    return False


def _try_enable_captions(page) -> bool:
    """Enable live captions IN THE MEETING and VERIFY the caption pane
    appeared. The 'Captions' entry in the More flyout opens a submenu whose
    'Turn on live captions' item is the actual toggle (docs/10 §4.4) — the
    previous version returned success at the 'Captions' click without ever
    checking, which is why every 2026-09-12 run ended
    captions_enable_failed/0-byte transcript despite the menu dump showing
    the item."""
    # direct toggle on the bar (if this UI exposes one)
    for label in ("Show live captions", "Turn on live captions",
                  "Turn on captions"):
        if _click_by_text(page, label) and _await_captions(page):
            return True
    # the More flyout path (proven entry point in the Sep 9 app-shell dumps)
    with contextlib.suppress(Exception):
        # ^More$ ONLY — run 8 evidence: name="More", exact=False matched the
        # app-shell sidebar's "Settings and more" gear and dumped its menu.
        page.get_by_role("button", name=re.compile(r"^(more|…)$",
                                                   re.I)).first.click(
            timeout=3000)
        page.wait_for_timeout(1200)  # flyout animation
        _dump_controls(page, "menu")
        # Older builds expose 'Captions' directly; the current one moved it
        # under 'Language and speech' / 'Record and transcribe' (run 9 flyout
        # dump: 'Record and transcribe | Language and speech | Settings').
        for entry in ("Captions", "Live captions", "Language and speech",
                      "Record and transcribe"):
            if not _click_by_text(page, entry):
                continue
            if _await_captions(page, seconds=4):
                return True  # some builds toggle immediately
            page.wait_for_timeout(800)
            _dump_controls(page, "captions_submenu")  # submenu evidence
            # Owner-supplied path (2026-09-12): More → Language and speech →
            # 'Show live captions'.
            for label in ("Show live captions", "Turn on live captions",
                          "Live captions", "Turn on captions", "Captions"):
                if _click_by_text(page, label) and _await_captions(page):
                    return True
            with contextlib.suppress(Exception):  # switch-style toggle
                sw = page.get_by_role(
                    "switch", name=re.compile(r"caption", re.I)).first
                if sw.count() > 0 and sw.is_visible():
                    sw.click(timeout=2000)
                    if _await_captions(page):
                        return True
            # entry didn't yield captions — back to the flyout root
            with contextlib.suppress(Exception):
                page.keyboard.press("Escape")
            page.get_by_role("button", name=re.compile(r"^(more|…)$",
                                                       re.I)).first.click(
                timeout=2000)
            page.wait_for_timeout(800)
        with contextlib.suppress(Exception):
            page.keyboard.press("Escape")
    logger.warning("captions_enable_failed")
    return False


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
        joined_identity = "unknown"
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
                # THE auth surface (probe 5): an EMBEDDED in-page dialog.
                # Fill/advance it every cycle and keep holding the guest join
                # while it progresses — auth completing removes the name field
                # and the Sign-in link, which frees the join below.
                if _drive_signin_dialog(candidate, settings):
                    signin_pending_until = max(
                        signin_pending_until,
                        min(join_deadline - 3, time.time() + 45))
                # mic/camera OFF before joining (owner: the listener never
                # broadcasts) — tid-anchored, text-verified, self-reverting.
                _turn_media_off_prejoin(candidate, media_off_tries)
                # Labelled toggles pass (covers the app-shell pre-join after
                # the auth hop): the classifier only fires on LIVE-state
                # labels ('Mute mic'/'Turn camera off'); 'Unmute mic'/'Turn
                # camera on' are off-state and untouched — idempotent.
                _mute_if_live(candidate)
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
                                joined_identity = (
                                    "authenticated"
                                    if "signin=n" in state
                                    and "name=none" in state else "guest")
                                logger.info("joined_as",
                                            identity=joined_identity)
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
        # Teams can REPLACE the join tab after the auth redirect — run 11's
        # crash: the tab whose 'Join now' we clicked was closed and the next
        # unguarded call threw TargetClosedError, killing the whole run.
        page = _resolve_live_page(context, page)
        with contextlib.suppress(Exception):
            if page.is_closed():
                logger.error("no_live_meeting_page")
                browser.close()
                return "join_lost_after_click"

        # LOBBY watch: a guest join waits on calling-lobby-screen until the
        # host admits (the authenticated join normally skips it). Surface it
        # loudly — a silent 'joined' in a lobby is NOT a working listener.
        in_lobby = False
        with contextlib.suppress(Exception):
            in_lobby = page.locator(
                "[data-tid='calling-lobby-screen']").count() > 0
        if in_lobby:
            logger.warning("listener_in_lobby", hint="host must admit")
            lobby_end = time.time() + 120
            while time.time() < lobby_end:
                with contextlib.suppress(Exception):
                    in_lobby = page.locator(
                        "[data-tid='calling-lobby-screen']").count() > 0
                if not in_lobby:
                    break
                time.sleep(3)
            logger.info("lobby_wait_over", still_waiting=in_lobby)

        # Wait for the REAL meeting bar (the 'Leave' control) — run 8's
        # caption attempt 5 s after join hit the app-shell sidebar instead
        # (its flyout dump: 'Settings and more' gear, not the call bar).
        with contextlib.suppress(Exception):
            page.wait_for_selector("button:has-text('Leave')", timeout=30_000)

        # Media OFF GUARANTEE (owner: never broadcast). The single pre-join
        # pass can race the bar's render, so RE-CHECK until the classifier
        # finds no live-state control — clicking anything still on. The
        # verified outcome also rides the join-proof caption below.
        media_clear = False
        for _ in range(8):
            if not _mute_if_live(page):
                media_clear = True
                break
            _safe_wait(page, 1500)
        logger.info("media_off_confirmed" if media_clear
                    else "media_off_UNCONFIRMED")
        _dump_controls(page, "joined")  # evidence for captions/bar selectors

        # Join proof: screenshot -> Telegram (owner's ask).
        _send_join_proof(page, joined_identity)

        # Captions (owner path: More → Language and speech → Show live
        # captions), retried until the caption pane is VERIFIED on screen.
        captions_on = False
        for _ in range(4):
            if _try_enable_captions(page):
                captions_on = True
                break
            _safe_wait(page, 6000)
        _open_chat_panel(page)      # form links land in the meeting chat

        transcript_file = _TRANSCRIPT_DIR / (
            "kyc_" + datetime.now().strftime("%Y%m%d_%H%M") + ".txt")
        transcript_file.parent.mkdir(parents=True, exist_ok=True)
        seen_captions: set[str] = set()
        transcript_handle = transcript_file.open("a", encoding="utf-8")

        relayed: set[str] = set()
        form_link: str | None = None
        all_segments: list[str] = []
        deadline = time.time() + max_minutes * 60
        next_caption_retry = time.time() + 30

        def _drain() -> None:
            for segment in _extract_captions(page, seen_captions):
                all_segments.append(segment)
                transcript_handle.write(segment + "\n")
                transcript_handle.flush()

        try:
            while time.time() < deadline:
                with contextlib.suppress(Exception):
                    if page.is_closed():
                        logger.warning("meeting_page_closed_mid_watch")
                        break
                if not captions_on and time.time() > next_caption_retry:
                    next_caption_retry = time.time() + 30
                    if _try_enable_captions(page):
                        captions_on = True
                _drain()
                for link in _extract_chat_links(page):
                    if link not in relayed:
                        relayed.add(link)
                        form_link = link
                        # Link + the teacher/presenter name heard in the
                        # captions so far (self-introductions) — owner's ask.
                        _relay_form_link(link, tuple(_presenter_names(
                            " ".join(all_segments))))
                if relayed:
                    # Form is up: brief grace for the trailing captions (the
                    # host usually names themselves around the link drop),
                    # then leave automatically — the relay has gone out.
                    leave_at = time.time() + 30
                    logger.info("form_found_leaving_in_30s")
                    while time.time() < leave_at:
                        _drain()
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
        presenters = tuple(_presenter_names(transcript_text))
        summary = _summarize(transcript_text, form_link, presenters)
        if presenters:
            summary = f"👤 Presenter: {', '.join(presenters)}\n" + summary
        if _telegram_send(text=summary):
            logger.info("session_summary_sent")
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
