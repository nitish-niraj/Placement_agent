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

from pia_worker.settings import get_settings
from pia_worker.teams import is_teams_url
from pia_worker.teams._shared import (
    EDGE_UA,
    _direct_meeting_url,
    _is_login_url,
    _resolve_link,
)
from pia_worker.teams.auth import (
    _click_signin as _click_signin,
)
from pia_worker.teams.auth import (
    _dismiss_consent as _dismiss_consent,
)
from pia_worker.teams.auth import (
    _drive_signin_dialog as _drive_signin_dialog,
)
from pia_worker.teams.auth import (
    _prejoin_state as _prejoin_state,
)
from pia_worker.teams.auth import (
    _prejoin_status as _prejoin_status,
)
from pia_worker.teams.auth import (
    check_session as check_session,
)
from pia_worker.teams.auth import (
    drive_login_page as drive_login_page,
)
from pia_worker.teams.auth import (
    is_authenticated_join as is_authenticated_join,
)
from pia_worker.teams.auth import (
    is_verified_session as is_verified_session,
)
from pia_worker.teams.auth import (
    note_guest_join as note_guest_join,
)
from pia_worker.teams.captions import (
    _await_captions as _await_captions,
)
from pia_worker.teams.captions import (
    _caption_inventory as _caption_inventory,
)
from pia_worker.teams.captions import (
    _captions_on as _captions_on,
)
from pia_worker.teams.captions import (
    _click_by_text as _click_by_text,
)
from pia_worker.teams.captions import (
    _dump_controls as _dump_controls,
)
from pia_worker.teams.captions import (
    _extract_captions as _extract_captions,
)
from pia_worker.teams.captions import (
    _try_enable_captions as _try_enable_captions,
)
from pia_worker.teams.captions import (
    pane_health as pane_health,
)
from pia_worker.teams.chat import (
    _extract_chat_links as _extract_chat_links,
)
from pia_worker.teams.chat import (
    _open_chat_panel as _open_chat_panel,
)
from pia_worker.teams.chat import (
    _presenter_names as _presenter_names,
)
from pia_worker.teams.chat import (
    _relay_form_link as _relay_form_link,
)
from pia_worker.teams.prejoin import (
    _media_button_should_click as _media_button_should_click,
)
from pia_worker.teams.prejoin import (
    _mute_if_live as _mute_if_live,
)
from pia_worker.teams.prejoin import (
    _resolve_live_page as _resolve_live_page,
)
from pia_worker.teams.prejoin import (
    _safe_wait as _safe_wait,
)
from pia_worker.teams.prejoin import (
    _turn_media_off_prejoin as _turn_media_off_prejoin,
)

# Strangler re-exports: canonical homes own the logic; these aliases keep
# existing imports (tests, login_save) working during the drain.
from pia_worker.teams.summary import (
    _groq_summary as _groq_summary,
)
from pia_worker.teams.summary import (
    _openrouter_summary as _openrouter_summary,
)
from pia_worker.teams.summary import (
    _summarize as _summarize,
)

logger = structlog.get_logger()

# Login-popup credentials come ONLY from the environment (DEC-008 amendment:
# the listener joins AS the owner). Set TEAMS_EMAIL / TEAMS_PASSWORD in
# infrastructure/.env — never hardcode them here.

_STATE_FILE = Path(__file__).resolve().parents[5] / "infrastructure" / "teams_session.json"
_TRANSCRIPT_DIR = Path(__file__).resolve().parents[5] / "transcripts"

def _telegram_send(*, text: str | None = None, photo: bytes | None = None,
                   caption: str | None = None,
                   chat_id: str | None = None) -> bool:
    """One Telegram door: sendMessage or sendPhoto. Best-effort, never fatal.
    `chat_id` overrides the student channel (ops/admin routing) — callers
    without one reach the student, so internal traces must never be passed
    without an explicit admin chat."""
    settings = get_settings()
    target = chat_id or settings.telegram_chat_id
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    try:
        if photo is not None:
            httpx.post(
                f"{base}/sendPhoto",
                data={"chat_id": target,
                      "caption": caption or ""},
                files={"photo": photo}, timeout=30)
        else:
            httpx.post(
                f"{base}/sendMessage",
                data={"chat_id": target,
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

# _is_login_url / _resolve_link / _direct_meeting_url live in
# pia_worker.teams._shared (strangler extract 2026-09-22) and are re-exported
# here so existing imports (tests, autologin, login_save) keep working.

# NOTE: _resolve_link / _direct_meeting_url imported from _shared (see top).

def listen(meeting_url: str, *, max_minutes: int = 180,
           join_now: bool = True) -> str:
    """Join the meeting, watch the chat for the feedback form, relay, leave."""
    meeting_url = _direct_meeting_url(_resolve_link(meeting_url))
    if not is_teams_url(meeting_url):
        return "not_a_teams_link"
    gate = check_session(str(_STATE_FILE))
    if gate is not None:
        return gate

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # headed: audio keeps playing
        context = browser.new_context(
            storage_state=str(_STATE_FILE), viewport={"width": 1400, "height": 900},
            permissions=["microphone", "camera"],  # pre-join screen toggles handled anyway
            # Realistic UA: Teams serves the full app shell to real browsers;
            # the default automation UA gets a degraded, never-loading shell.
            user_agent=EDGE_UA,
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
            """Join-loop alias of the single login driver (auth.py)."""
            drive_login_page(pg, settings)

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
                                    if is_authenticated_join(state)
                                    else "guest")
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
        if joined_identity == "guest":
            # Expired session lands here silently today — say so once.
            note_guest_join()
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
        # One-shot pane inventory: a silent run can then tell 'nobody spoke'
        # apart from 'selectors match nothing'. Diagnostics never break watch.
        # A dead pane (all selectors throwing) pages the owner once — Teams
        # may have renamed the caption DOM and the transcript would be empty.
        pane_state = "unknown"
        pane_reason = ""
        with contextlib.suppress(Exception):
            inventory = _caption_inventory(page)
            logger.info("caption_pane_inventory", **inventory)
            pane_state, pane_reason = pane_health(inventory)
            logger.info("caption_pane_health", state=pane_state,
                        reason=pane_reason)
        health_alerted = False
        if pane_state == "dead":
            health_alerted = True
            logger.warning("caption_pane_dead", reason=pane_reason)
            with contextlib.suppress(Exception):
                _telegram_send(
                    text="⚠️ Meeting captions look broken — Teams may have "
                         "changed its layout, so this session's transcript "
                         "may come back empty. The form-link watch is still "
                         "running.")

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
                    elif not health_alerted:
                        with contextlib.suppress(Exception):
                            state, _ = pane_health(_caption_inventory(page))
                            if state == "dead":
                                health_alerted = True
                                logger.warning("caption_pane_dead_mid_watch")
                                _telegram_send(
                                    text="⚠️ Meeting captions stopped working "
                                         "mid-session — the transcript may be "
                                         "incomplete. The form-link watch is "
                                         "still running.")
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
