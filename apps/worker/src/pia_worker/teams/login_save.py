"""One-time Teams session saver (DEC-008 amendment: Type 1 informational KYC
personal listener — owner's own credentials).

Run locally, headed (a real browser window opens):
    .venv/Scripts/python -m pia_worker.teams.login_save

Sign in with YOUR university Microsoft account (complete MFA). Once the Teams
app shell loads, the browser session is saved to
infrastructure/teams_session.json (gitignored, SEC-001) and reused by the
listener bot — the password is never stored anywhere.

Session expiry: if the listener later reports "login needed", re-run this.
"""

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# Host-run tool: .../apps/worker/src/pia_worker/teams/login_save.py → repo root
# is five parents up. (This script never runs in a container.)
_REPO_ROOT = Path(__file__).resolve().parents[5]
_STATE_FILE = _REPO_ROOT / "infrastructure" / "teams_session.json"


def main() -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()
        page.goto("https://teams.microsoft.com/")
        print("Log in with your university account (complete MFA).")
        print("The browser stays open until you are signed in (up to 5 minutes).")

        # Wait for a SIGNED-IN state: the URL must leave the login pages AND
        # reach the Teams v2 shell. Matching the bare teams.microsoft.com URL
        # was the bug — it matches before login and saves an empty session.
        deadline = time.time() + 300
        signed_in = False
        while time.time() < deadline:
            url = page.url
            if ("login.microsoftonline" not in url and "login.live" not in url
                    and "/signin" not in url and "/v2/" in url):
                page.wait_for_timeout(4000)  # let the app shell settle
                if "login" not in page.url:
                    signed_in = True
                    break
            page.wait_for_timeout(2000)
        if not signed_in:
            print("Timed out waiting for sign-in — nothing saved. Try again.")
            browser.close()
            return

        context.storage_state(path=str(_STATE_FILE))
        print(f"Signed in — session saved: {_STATE_FILE} "
              f"({_STATE_FILE.stat().st_size} bytes)")
        browser.close()


if __name__ == "__main__":
    main()
