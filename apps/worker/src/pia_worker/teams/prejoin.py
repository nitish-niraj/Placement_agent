"""Pre-join orchestration helpers (strangler extract from listener.py, 2026-09-23).

Media-toggle classification, mic/camera-off guarantee, live-tab resolution.
Depends on auth.py for pre-join state — never the reverse. listener.py
re-exports these names so existing imports keep working.
"""

import contextlib
import re
import time

import structlog

from pia_worker.teams.auth import _prejoin_status

logger = structlog.get_logger()

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


def _mute_if_live(page) -> bool:  # noqa: ANN001
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
