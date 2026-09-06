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

        # Auto-detect: URL leaves the login pages and sits on teams.microsoft.
        deadline = time.time() + 300
        interrupted = False
        signed_in = False
        last_reported: str | None = None
        try:
            while time.time() < deadline:
                url = page.url
                if url != last_reported:
                    _say(f"  current page: {url[:100]}")
                    last_reported = url
                    if "login.microsoftonline" in url:
                        _say("  (if a 'Stay signed in?' prompt is showing, click Yes)")
                on_login = _on_login_page(url)
                if not on_login and "teams.microsoft" in url:
                    time.sleep(4)  # let the app shell settle
                    if not _on_login_page(page.url):
                        signed_in = True
                        _say("Signed-in state detected — saving.")
                        break
                time.sleep(2)
        except KeyboardInterrupt:
            interrupted = True
            _say("Interrupted — saving session if you are signed in…")

        # Final evaluation works for both the auto-detect and the Ctrl+C path.
        try:
            url = page.url
        except Exception:  # noqa: BLE001 — browser already gone
            url = ""
        signed_in = signed_in or (
            url and "teams.microsoft" in url and not _on_login_page(url))
        if not signed_in:
            _say("Not signed in (or still on a login page) — nothing saved. "
                 "Complete the login and try again.")
            with contextlib.suppress(Exception):
                browser.close()
            return

        context.storage_state(path=str(_STATE_FILE))
        _say(f"Signed in — session saved: {_STATE_FILE} "
             f"({_STATE_FILE.stat().st_size} bytes)"
             + (" (saved via Ctrl+C)" if interrupted else ""))
        with contextlib.suppress(Exception):
            browser.close()


if __name__ == "__main__":
    main()
