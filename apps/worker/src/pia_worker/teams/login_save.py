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

from pathlib import Path

from playwright.sync_api import sync_playwright

_STATE_FILE = Path(__file__).resolve().parents[3] / "infrastructure" / "teams_session.json"


def main() -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()
        page.goto("https://teams.microsoft.com/")
        print("Log in with your university account (complete MFA).")
        print("Waiting for the Teams app shell to load... (up to 5 minutes)")
        # Signed-in signal: the Teams shell renders the app bar / chat surface.
        page.wait_for_url("**/teams.microsoft.com/**", timeout=300_000)
        page.wait_for_timeout(8000)  # let the app settle before freezing cookies
        context.storage_state(path=str(_STATE_FILE))
        print(f"Session saved: {_STATE_FILE}")
        browser.close()


if __name__ == "__main__":
    main()
