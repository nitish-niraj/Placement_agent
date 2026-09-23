"""Pre-join authentication (strangler extract from listener.py, 2026-09-23).

Sign-in controls, the embedded Microsoft dialog driver, the popup login
driver (deduped: listener's join loop and login_save shared one copy each —
now single), consent guard, pre-join DOM truth, and the session-validity
probe. listener.py re-exports these names so existing imports keep working.
"""

import contextlib
import json
import re
import time
from pathlib import Path

import structlog

from pia_worker.teams._shared import _is_login_url

logger = structlog.get_logger()


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


def is_authenticated_join(state: str) -> bool:
    """State-only auth signal: the name box AND the Sign-in link are both
    gone (the authenticated pre-join has neither). Used for the join-identity
    label — guest screens hold the join while a hop is pending."""
    return "signin=n" in state and "name=none" in state


def is_verified_session(state: str, body: str, email: str) -> bool:
    """Full session proof for session saves: authenticated join AND the
    official address visible on screen (wrong-account guard)."""
    return (is_authenticated_join(state) and "joinnow=on" in state
            and bool(email) and email.lower() in (body or "").lower())


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
    cookies' that blocked joins IS that login dialog (footer text). The old
    consent-dismiss code was CLOSING it — self-sabotage. _dismiss_consent now
    never touches it while a dialog input is present.
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


def drive_login_page(pg, settings, say=None) -> None:  # noqa: ANN001
    """Single Microsoft-login-form driver (deduped 2026-09-23: the
    listener's join loop and login_save carried identical copies).
    Completes popup / same-tab / iframe forms: email -> password ->
    Yes/Next/Accept. Structured events always log; `say` (login_save's
    console printer) additionally gets human-readable lines."""
    with contextlib.suppress(Exception):
        email_box = pg.locator(
            "input[type=email], input[name=loginfmt]").first
        if (email_box.count() > 0 and email_box.is_visible()
                and settings.teams_email):
            email_box.fill(settings.teams_email)
            pg.locator("#idSIButton9, input[type=submit], "
                       "button[type=submit]").first.click(timeout=2000)
            logger.info("login_email_submitted")
            if say is not None:
                say("login email submitted")
            time.sleep(2)
    with contextlib.suppress(Exception):
        pw_box = pg.locator("input[type=password]").first
        if (pw_box.count() > 0 and pw_box.is_visible()
                and settings.teams_password):
            pw_box.fill(settings.teams_password)
            pg.locator("#idSIButton9, input[type=submit], "
                       "button[type=submit]").first.click(timeout=2000)
            logger.info("login_password_submitted")
            if say is not None:
                say("login password submitted")
            time.sleep(2)
    for label in ("Yes", "Next", "Accept"):
        with contextlib.suppress(Exception):
            btn = pg.get_by_role("button", name=label, exact=True).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=1500)
                logger.info("login_prompt_answered", label=label)
                if say is not None:
                    say(f"login prompt answered: {label}")


# Microsoft/Teams auth-cookie domains that prove a saved session is alive.
_SESSION_COOKIE_DOMAINS = (
    "login.microsoftonline", "teams.microsoft", "teams.live",
    "teams.cloud.microsoft", "microsoftonline.com",
)

_SESSION_EXPIRED_NOTE = (
    "⚠️ Teams session expired — I couldn't verify the saved sign-in, so "
    "re-run `login_save` on the host PC to sign in again. "
    "Today's joins may land as guest until then.")
_GUEST_JOIN_NOTE = (
    "ℹ️ Joined today's session as guest (unverified identity) — the saved "
    "Teams sign-in may have expired. Re-run `login_save` if this repeats.")


def session_status(state_file: str | Path) -> tuple[str, str]:
    """Validity probe for a saved Playwright storage_state WITHOUT launching
    a browser: ok | expired | missing + human reason.

    ok = a session cookie for a Microsoft/Teams domain expires more than an
    hour out. Anything else is expired (safe direction: re-login rather than
    silently joining as guest) except a missing/unreadable file. Cookie
    presence is a heuristic — Microsoft rotates names — so this gates the
    alert, never blocks a join attempt on its own.
    """
    path = Path(state_file)
    if not path.is_file():
        return ("missing", "no saved session — run login_save first")
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_probe_unreadable", error=str(exc)[:120])
        return ("expired", "saved session is unreadable — re-login")
    now = time.time()
    best: float | None = None
    for cookie in stored.get("cookies", []) or []:
        domain = str(cookie.get("domain") or "")
        if not any(marker in domain for marker in _SESSION_COOKIE_DOMAINS):
            continue
        exp = cookie.get("expires", -1)
        if isinstance(exp, (int, float)) and exp > 0:
            best = exp if best is None else max(best, exp)
    if best is None:
        return ("expired", "no Microsoft session cookie in the saved file")
    if best - now > 3600:
        hours = int((best - now) // 3600)
        return ("ok", f"session cookie valid ~{hours}h more")
    return ("expired", "session cookie expires within the hour")


def check_session(state_file: str | Path, *, alert=None) -> str | None:
    """Pre-launch gate for listen(): probe the saved session, alert in plain
    language on expiry, and return the outcome string ("login_needed" /
    "session_expired") — or None when the join may proceed. `alert` defaults
    to listener's Telegram door (lazy import: auth never imports listener
    at module load). The probe gates the ALERT, never the attempt: an
    "expired" verdict still tries the join (env creds may recover it), but
    the owner is told instead of discovering a guest join afterwards."""
    from pia_worker.teams import listener as _listener  # noqa: PLC0415

    send = alert if alert is not None else _listener._telegram_send
    status, reason = session_status(state_file)
    if status == "missing":
        return "login_needed: run pia_worker.teams.login_save first"
    if status == "expired":
        logger.warning("teams_session_expired", reason=reason)
        send(text=_SESSION_EXPIRED_NOTE)
        return None  # attempt anyway — creds may still recover the hop
    logger.info("teams_session_ok", reason=reason)
    return None


def note_guest_join(*, alert=None) -> None:
    """One plain-language note when a run lands as guest identity."""
    from pia_worker.teams import listener as _listener  # noqa: PLC0415

    send = alert if alert is not None else _listener._telegram_send
    logger.warning("listener_joined_as_guest")
    send(text=_GUEST_JOIN_NOTE)
