"""One-time Teams session saver (DEC-008 amendment: Type 1 informational KYC
personal listener — owner's own credentials).

Run locally, headed (a real browser window opens):
    .venv/Scripts/python -m pia_worker.teams.login_save [meeting-url]

Phase 1 — sign in with YOUR university Microsoft account on the Teams app
(complete MFA, click Yes on "Stay signed in").
Phase 2 — if a MEETING URL is given, the same window then opens that meeting
so the CONSUMER sign-in hop gets trained too: click 'Sign in' on the pre-join,
complete the login, ACCEPT the 'Privacy and cookies' notice (live probes
2026-09-12: the listener can never get past that notice — the consent cookie
must be baked into the session here, once, by a human), and leave the
authenticated pre-join on screen. Auto-detected, then saved; Ctrl+C after
you're in saves anyway. Session: infrastructure/teams_session.json
(gitignored, SEC-001) — the password is never stored. Re-run whenever the
listener joins as guest/"Unverified" instead of your account.
"""

import argparse
import contextlib
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from pia_worker.teams import is_teams_url
from pia_worker.teams._shared import EDGE_UA

# Host-run tool: .../apps/worker/src/pia_worker/teams/login_save.py → repo root
# is five parents up. (This script never runs in a container.)
_REPO_ROOT = Path(__file__).resolve().parents[5]
_STATE_FILE = _REPO_ROOT / "infrastructure" / "teams_session.json"


def _say(msg: str) -> None:
    print(msg, flush=True)


def _on_login_page(url: str) -> bool:
    return ("login.microsoftonline" in url or "login.live" in url
            or "/signin" in url)


def _expected_account() -> str:
    """The OFFICIAL university address from infrastructure/.env — the owner's
    stipulation: a session trained under any other account is not acceptable."""
    with contextlib.suppress(Exception):
        for line in (_REPO_ROOT / "infrastructure" / ".env").read_text(
                encoding="utf-8").splitlines():
            if line.startswith("TEAMS_EMAIL="):
                return line.split("=", 1)[1].strip()
    return ""


def main() -> None:
    """login_save [meeting-url] [--auto --meeting URL --name NAME]

    Manual (default): phase 1 org app login (always); phase 2 trains the
    consumer sign-in hop + privacy-consent cookie on the given meeting URL
    (skipped when no URL is passed).
    Auto (--auto): programmatic session refresh reusing the listener's
    sign-in dialog driver (replaces archived E:/agent/autologin.py — no
    hardcoded paths, URLs, or names). MFA still needs one phone tap.
    """
    parser = argparse.ArgumentParser(description="Teams session saver")
    parser.add_argument("meeting_url", nargs="?",
                        help="meeting URL for manual phase-2 training")
    parser.add_argument("--auto", action="store_true",
                        help="drive the Microsoft login forms programmatically")
    parser.add_argument("--meeting", default="",
                        help="meeting URL for --auto (or TEAMS_MEETING_URL)")
    parser.add_argument("--name", default="Nitish Kumar",
                        help="guest display name for --auto pre-join")
    parser.add_argument("--evidence-out", default="",
                        help="evidence dump path for --auto "
                             "(default: transcripts/autologin_evidence_<ts>.txt)")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.auto:
        _auto_refresh(meeting=args.meeting, name=args.name,
                      evidence_out=args.evidence_out,
                      timeout_seconds=args.timeout_seconds)
        return
    meeting_url = args.meeting_url or ""
    _manual_save(meeting_url)


def _manual_save(meeting_url: str) -> None:
    """Manual flow (unchanged): human completes the login in a headed window."""
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            # Same realistic Edge UA the listener needs — Teams serves a
            # degraded, never-loading shell to the default automation UA.
            user_agent=EDGE_UA,
            locale="en-IN",
        )
        page = context.new_page()
        page.goto("https://teams.microsoft.com/")
        _say("Log in with your university account (complete MFA, click Yes on "
             "'Stay signed in').")
        _say("Teams loads → saved automatically. Ctrl+C after you're in also works.")

        # Auto-detect (two-signal rule): the /v2/ landing page can sit on the
        # Teams domain with a "Sign in" button for anonymous visitors — so the
        # URL must hold for 20 continuous seconds AND no "Sign in" control may
        # be visible in the DOM. The authenticated app has neither.
        deadline = time.time() + 900  # 15 minutes: take your time on MFA
        signed_in = False
        last_reported: str | None = None
        stable_since: float | None = None
        prompted = False
        last_beat = time.time()
        try:
            while time.time() < deadline:
                if time.time() - last_beat > 60:
                    last_beat = time.time()
                    _say(f"  (window still open — {int(deadline - time.time())}"
                         "s left; complete the LPU login)")
                # Microsoft sometimes completes in a DIFFERENT tab (login
                # popup) than the page we started — track the live one.
                for pg2 in context.pages:
                    with contextlib.suppress(Exception):
                        if is_teams_url(pg2.url) and not _on_login_page(
                                pg2.url):
                            page = pg2
                            break
                url = page.url
                if url != last_reported:
                    _say(f"  current page: {url[:100]}")
                    last_reported = url
                    if _on_login_page(url) and not prompted:
                        _say("  (complete ID + password + MFA; click Yes on "
                             "'Stay signed in'; if the page shows a Sign in "
                             "button, CLICK IT first)")
                        prompted = True
                on_login = _on_login_page(url)
                if not on_login and is_teams_url(url):
                    with contextlib.suppress(Exception):
                        sign_in_control = (
                            page.get_by_role("link", name="Sign in").count()
                            + page.get_by_role("button", name="Sign in").count())
                    if sign_in_control > 0:
                        stable_since = None
                        if not prompted:
                            _say('Landing page shows "Sign in" — click it and '
                                 "complete the login.")
                            prompted = True
                    else:
                        if stable_since is None:
                            stable_since = time.time()
                        elif time.time() - stable_since >= 20:
                            signed_in = True
                            _say("Signed-in app confirmed (20s, no Sign in "
                                 "control) — saving.")
                            break
                else:
                    stable_since = None
                time.sleep(2)
        except KeyboardInterrupt:
            # Ctrl+C = "I'm in, save now" — but verify with a fresh navigation:
            # an anonymous visitor bounces to login within seconds; signed-in
            # stays. This is what prevents saving a junk session again.
            _say("Interrupted — verifying you are really signed in…")
            with contextlib.suppress(Exception):
                page.goto("https://teams.microsoft.com/v2/", timeout=30_000)
                time.sleep(8)

        try:
            url = page.url
        except Exception:  # noqa: BLE001 — browser already gone
            url = ""
        signed_in = (
            bool(url) and "teams.microsoft" in url and not _on_login_page(url))
        if not signed_in:
            _say("Not signed in (bounced to the login wall) — nothing saved. "
                 "Complete the login and try again.")
            with contextlib.suppress(Exception):
                browser.close()
            return

        # --- Phase 2: train the consumer meeting hop + consent cookie ------
        if meeting_url:
            from pia_worker.teams.listener import (  # noqa: PLC0415
                _direct_meeting_url,
                _resolve_link,
            )
            direct = _direct_meeting_url(_resolve_link(meeting_url))
            if not is_teams_url(direct):
                _say(f"Not a Teams URL after resolving ({direct[:60]}) — "
                     "skipping phase 2.")
            else:
                try:
                    page.goto(direct, timeout=60_000)
                except Exception as exc:  # noqa: BLE001
                    _say(f"  (meeting page hiccup: {str(exc)[:80]})")
                _say("Phase 2: on the meeting PRE-JOIN screen, click "
                     "'Sign in' → complete the Microsoft")
                _say("  login → ACCEPT the 'Privacy and cookies' notice "
                     "(Next → Accept) — this is")
                _say("  the consent cookie the listener cannot get past — → "
                     "Yes on 'Stay signed in'.")
                expected = _expected_account()
                if expected:
                    _say(f"  You MUST be on the OFFICIAL account {expected} "
                         "— the session will only")
                    _say("  be saved when that address is visible on the "
                         "authenticated pre-join.")
                _say("  Leave the window on the pre-join that shows YOUR "
                     "ACCOUNT (no name box).")
                end2 = time.time() + 420  # 7 minutes for the human steps
                verified = False
                try:
                    while time.time() < end2:
                        signin = name_box = join = False
                        with contextlib.suppress(Exception):
                            signin = page.locator(
                                "[data-tid='auth-sign-in-link']").is_visible()
                            name_box = page.locator(
                                "[data-tid='prejoin-display-name-input']"
                            ).is_visible()
                            join = page.locator(
                                "[data-tid='prejoin-join-button']").is_visible()
                        if join and not signin and not name_box:
                            body = ""
                            with contextlib.suppress(Exception):
                                body = (page.text_content("body") or "")
                            if (not expected
                                    or expected.lower() in body.lower()):
                                _say("Authenticated pre-join on the official "
                                     "account — saving.")
                                verified = True
                                break
                            _say(f"  Authenticated, but {expected} is NOT on "
                                 "screen — wrong account.")
                            _say("  Sign out / 'Use another account' and "
                                 "complete the LPU login.")
                        time.sleep(3)
                except KeyboardInterrupt:
                    _say("Interrupted — Ctrl+C accepted; saving as-is.")
                    verified = True
                else:
                    if not verified:
                        _say("Phase-2 window ended without the OFFICIAL "
                             "authenticated pre-join — refusing to overwrite "
                             "the session file. Re-run and complete the LPU "
                             "login + notice acceptance.")
                        with contextlib.suppress(Exception):
                            browser.close()
                        return

        context.storage_state(path=str(_STATE_FILE))
        _say(f"Signed in — session saved: {_STATE_FILE} "
             f"({_STATE_FILE.stat().st_size} bytes)")
        with contextlib.suppress(Exception):
            browser.close()


def _drive_login_page(pg, settings) -> None:  # noqa: ANN001
    """Console-facing alias of the single login driver (auth.py) — kept for
    backward compatibility; new code calls auth.drive_login_page directly."""
    from pia_worker.teams.auth import drive_login_page

    drive_login_page(pg, settings, say=_say)


def _auto_refresh(*, meeting: str, name: str, evidence_out: str,
                  timeout_seconds: int) -> None:
    """Programmatic refresh (ex-archived autologin.py, deduped 2026-09-22).

    Drives the pre-join Sign-in link + embedded Microsoft dialog with env
    credentials, verifies the official LPU address on the authenticated
    pre-join, briefly joins to dump the meeting-bar controls, leaves, and
    saves the session. Never saves on failure. MFA needs a phone tap.
    """
    import os

    from pia_worker.settings import get_settings
    from pia_worker.teams._shared import (
        _direct_meeting_url,
        _is_login_url,
        _resolve_link,
    )
    from pia_worker.teams.auth import (
        _drive_signin_dialog,
        _prejoin_state,
        drive_login_page,
    )

    settings = get_settings()
    email = (settings.teams_email or os.environ.get("TEAMS_EMAIL", "")).strip()
    password = (settings.teams_password
                or os.environ.get("TEAMS_PASSWORD", "")).strip()
    meeting = (meeting or os.environ.get("TEAMS_MEETING_URL", "")).strip()
    if not email or not password:
        raise SystemExit("missing TEAMS_EMAIL/TEAMS_PASSWORD — source "
                         "infrastructure/.env first")
    if not meeting or not is_teams_url(_resolve_link(meeting)):
        raise SystemExit("missing/invalid --meeting Teams URL")
    direct = _direct_meeting_url(_resolve_link(meeting))
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if evidence_out:
        dump = Path(evidence_out)
    else:
        dump = (_REPO_ROOT / "transcripts" /
                ("autologin_evidence_"
                 + datetime.now().strftime("%Y%m%d_%H%M%S") + ".txt"))
    dump.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        kwargs = {"viewport": {"width": 1400, "height": 900},
                  "permissions": ["microphone", "camera"],
                  "user_agent": EDGE_UA, "locale": "en-IN"}
        if _STATE_FILE.exists():
            kwargs["storage_state"] = str(_STATE_FILE)
        ctx = browser.new_context(**kwargs)  # type: ignore[arg-type]
        page = ctx.new_page()
        _say(f"opening {direct[:90]}")
        page.goto(direct, timeout=90_000)
        page.wait_for_timeout(15_000)

        with contextlib.suppress(Exception):
            page.locator(
                "[data-tid='prejoin-display-name-input']").first.fill(name)
        with contextlib.suppress(Exception):
            from pia_worker.teams.auth import _click_signin
            if _click_signin(page):
                _say("sign-in link clicked")
        page.wait_for_timeout(5000)

        # Consent notice: physical clicks time out on the overlay — use
        # JS el.click() (archived trainer lesson). Page Next until Accept.
        for cycle in range(12):
            gone = True
            with contextlib.suppress(Exception):
                gone = page.get_by_text(
                    "Privacy and cookies", exact=False).count() == 0
            if gone:
                _say(f"cycle {cycle}: consent gone")
                break
            els = []
            with contextlib.suppress(Exception):
                els = page.query_selector_all("button")
            target = None
            for word in ("accept all", "accept", "allow all", "i agree",
                         "confirm choices", "next"):
                for b in els:
                    with contextlib.suppress(Exception):
                        if (b.is_visible() and (b.text_content() or "")
                                .strip().lower() == word):
                            target = (b, word)
                            break
                if target:
                    break
            if target is None:
                with contextlib.suppress(Exception):
                    texts = [(b.text_content() or "").strip()
                             for b in els if b.is_visible()][:14]
                    _say(f"cycle {cycle}: no target, buttons={texts}")
            else:
                b, word = target
                with contextlib.suppress(Exception):
                    b.evaluate("el => el.click()")
                    _say(f"cycle {cycle}: JS-clicked '{word}'")
            page.wait_for_timeout(2000)

        deadline = time.time() + max(60, timeout_seconds)
        authed = False
        last_beat = 0.0
        while time.time() < deadline and not authed:
            for pg2 in list(ctx.pages):
                if pg2.is_closed():
                    continue
                if _is_login_url(pg2.url):
                    drive_login_page(pg2, settings, say=_say)
            for fr in page.frames:
                with contextlib.suppress(Exception):
                    if _is_login_url(fr.url):
                        drive_login_page(fr, settings, say=_say)
            with contextlib.suppress(Exception):
                _drive_signin_dialog(page, settings)
            state = _prejoin_state(page)
            if time.time() - last_beat > 10:
                last_beat = time.time()
                _say(f"state: {state[:160]}")
            body = ""
            with contextlib.suppress(Exception):
                body = page.text_content("body") or ""
            from pia_worker.teams.auth import (
                is_authenticated_join,
                is_verified_session,
            )
            if is_verified_session(state, body, email):
                _say(f"AUTHENTICATED PRE-JOIN — {email} present.")
                break
            if "joinnow=on" in state and is_authenticated_join(state):
                _say("authenticated pre-join but LPU email missing — "
                     "wrong account?!")
            page.wait_for_timeout(3000)

        with dump.open("w", encoding="utf-8") as f:
            f.write(_prejoin_state(page) + "\n")
            with contextlib.suppress(Exception):
                f.write("visible: " + " | ".join(dict.fromkeys(
                    (b.text_content() or b.get_attribute("aria-label")
                     or "").strip()
                    for b in page.query_selector_all("button")
                    if b.is_visible())))

        if authed:
            with contextlib.suppress(Exception):
                page.locator(
                    "[data-tid='prejoin-join-button']").first.click(
                        timeout=5000)
                _say("joined authenticated (evidence capture)")
                page.wait_for_timeout(12000)
                labels = []
                with contextlib.suppress(Exception):
                    for b in page.query_selector_all(
                            "button, [role=menuitem]"):
                        if b.is_visible():
                            lab = ((b.get_attribute("aria-label")
                                    or b.text_content() or "").strip())
                            if lab and len(lab) < 60:
                                labels.append(lab)
                with dump.open("a", encoding="utf-8") as f:
                    f.write("\n\nMEETING BAR: " + " | ".join(
                        dict.fromkeys(labels)))
                with contextlib.suppress(Exception):
                    page.screenshot(path=str(dump.with_suffix(".png")))
                with contextlib.suppress(Exception):
                    page.get_by_role("button", name="Leave").first.click(
                        timeout=3000)
            ctx.storage_state(path=str(_STATE_FILE))
            _say(f"SESSION SAVED ({_STATE_FILE.stat().st_size} bytes) -> "
                 f"{dump.name}")
        else:
            _say(f"NOT authenticated — session NOT saved. See {dump}")
        browser.close()


if __name__ == "__main__":
    main()
