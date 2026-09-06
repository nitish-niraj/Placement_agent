"""One-time Teams session saver (DEC-008 amendment: Type 1 informational KYC
personal listener — owner's own credentials).

Run locally, headed (a real browser window opens):
    .venv/Scripts/python -m pia_worker.teams.login_save

Sign in with YOUR university Microsoft account (complete MFA, click Yes on
"Stay signed in"). The script auto-detects when Teams has loaded — and if it
doesn't, pressing Ctrl+C after you are in saves the session anyway. The
session goes to infrastructure/teams_session.json (gitignored, SEC-001); the
password is never stored anywhere. Re-run when the session expires.
"""

import contextlib
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# Host-run tool: .../apps/worker/src/pia_worker/teams/login_save.py → repo root
# is five parents up. (This script never runs in a container.)
_REPO_ROOT = Path(__file__).resolve().parents[5]
_STATE_FILE = _REPO_ROOT / "infrastructure" / "teams_session.json"


def _say(msg: str) -> None:
    print(msg, flush=True)


def _on_login_page(url: str) -> bool:
    return ("login.microsoftonline" in url or "login.live" in url
            or "/signin" in url)


def main() -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()
        page.goto("https://teams.microsoft.com/")
        _say("Log in with your university account (complete MFA, click Yes on "
             "'Stay signed in').")
        _say("Teams loads → saved automatically. Ctrl+C after you're in also works.")

        # Auto-detect (two-signal rule): the /v2/ landing page can sit on the
        # Teams domain with a "Sign in" button for anonymous visitors — so the
        # URL must hold for 20 continuous seconds AND no "Sign in" control may
        # be visible in the DOM. The authenticated app has neither.
        deadline = time.time() + 300
        signed_in = False
        last_reported: str | None = None
        stable_since: float | None = None
        prompted = False
        try:
            while time.time() < deadline:
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
                if not on_login and "teams.microsoft" in url:
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

        context.storage_state(path=str(_STATE_FILE))
        _say(f"Signed in — session saved: {_STATE_FILE} "
             f"({_STATE_FILE.stat().st_size} bytes)")
        with contextlib.suppress(Exception):
            browser.close()


if __name__ == "__main__":
    main()
