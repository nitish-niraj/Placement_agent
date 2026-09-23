"""In-meeting chat watch (strangler extract from listener.py, 2026-09-23).

Form-link relay, presenter-name mining, and the docked chat panel opener.
The Telegram send stays behind listener's door via a call-time import, so
this module never creates an import cycle. listener.py re-exports these
names so existing imports keep working.
"""

import contextlib
import re

import structlog

from pia_worker.events.rules import FORM_LINK_PATTERN

logger = structlog.get_logger()

_FORM_LINK = FORM_LINK_PATTERN  # P13/F-029: one shared detector (events/rules)
_FORM_RE = None  # compiled lazily inside watch loop
_CHAT_SELECTORS = [
    "[role='list'] [data-tid='chat-pane-item']",  # best-effort; Teams DOM shifts
    "div[data-tid='message-body-content']",
]

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
    himself), plus the teacher/presenter name if the session revealed one.
    The same link also becomes an approval-gated draft on the dashboard
    (P13/F-030, DEC-008 Amendment 2026-09-13) — best-effort: a DB hiccup
    must never break the relay or the session."""
    from pia_worker.teams.listener import _telegram_send  # noqa: PLC0415

    who = (", ".join(presenters) if presenters
           else "not detected in captions/chat yet")
    text = (
        "📝 <b>KYC feedback form is up</b>\n"
        f"<a href='{link}'>Fill it now (manually — always yours)</a>\n"
        f"👤 Teacher/presenter: {who}\n"
        "Also queued as a pre-filled draft — review it on the dashboard "
        "(Approvals).\n"
        "The listener will leave the session shortly.")
    if _telegram_send(text=text):
        logger.info("form_link_relayed", link=link[:60], presenters=who)
    try:
        from pia_worker.jobs.propose_actions import propose_meeting_form_action

        outcome = propose_meeting_form_action(link, presenters=presenters)
        logger.info("form_draft_proposed", outcome=outcome, link=link[:60])
    except Exception as exc:  # noqa: BLE001 — the relay already went out
        logger.warning("form_draft_proposal_failed", error=str(exc)[:120])


def _extract_chat_links(page) -> list[str]:  # noqa: ANN001
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


def _open_chat_panel(page) -> None:  # noqa: ANN001
    """Open the DOCKED in-meeting chat panel (toolbar Chat) and stay in the
    meeting — captions and the chat inbox are then watched together every
    watch cycle. Clicking the app-shell Chat nav instead leaves the meeting
    view (mini-window mode): the caption pane unmounts and the transcript
    comes back empty. Candidates inside a <nav> landmark are therefore
    skipped; afterwards the meeting bar (Leave) must still be on screen."""
    try:
        candidates = page.get_by_role(
            "button", name=re.compile(r"chat", re.IGNORECASE)).all()
    except Exception:  # noqa: BLE001 — locator failure falls to legacy path
        candidates = []
    for btn in candidates:
        try:
            if btn.evaluate("el => el.closest('nav') ? 1 : 0"):
                continue  # app-shell nav — would leave the meeting
            if btn.is_visible() and btn.is_enabled():
                btn.click(timeout=2500)
                logger.info("meeting_chat_docked")
                page.wait_for_timeout(1500)
                break
        except Exception:  # noqa: BLE001 — try the next candidate
            continue
    else:
        # Legacy single-click path (may land on the app-shell chat page).
        with contextlib.suppress(Exception):
            page.get_by_role("button", name="Chat", exact=False).first.click(
                timeout=2500)
            logger.info("chat_panel_opened")
            page.wait_for_timeout(1500)
    with contextlib.suppress(Exception):
        if page.get_by_role("button", name="Leave").count():
            logger.info("stayed_in_meeting")
        else:
            logger.warning("meeting_view_lost_after_chat_open")
