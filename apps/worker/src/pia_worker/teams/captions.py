"""Live-captions enable + scrape (strangler extract from listener.py, 2026-09-23).

Toggle-aware state machine, junk-filtered scraping, pane inventory, and the
pure pane-health assessment (2F alerting builds on it). listener.py
re-exports these names so existing imports keep working.
"""

import contextlib
import re
import time
from datetime import datetime
from pathlib import Path

import structlog

logger = structlog.get_logger()

_TRANSCRIPT_DIR = Path(__file__).resolve().parents[5] / "transcripts"

_CAPTION_SELECTORS = [
    "span[data-tid='closed-caption-text']",
    "[data-tid='closed-caption'] span",
    "[data-tid*='caption']",
    "[aria-label*='aption']",
]

# System strings the caption pane renders when nobody is speaking — never
# transcript. Matched case-insensitively as substring (they are UI chrome,
# never speech).
_CAPTION_JUNK = (
    "captions will be shown in",
    "live captions are on",
    "caption language",
    "turn on captions",
    "captions are off",
)

_PRIMARY_SELECTOR = "span[data-tid='closed-caption-text']"


def _extract_captions(page, seen: set[str]) -> list[str]:  # noqa: ANN001
    """New live-caption segments from the captions pane (best-effort DOM)."""
    new: list[str] = []
    for selector in _CAPTION_SELECTORS:
        try:
            for el in page.query_selector_all(selector):
                text = (el.text_content() or "").strip()
                lowered = text.lower()
                if not text or text in seen:
                    continue
                if any(junk in lowered for junk in _CAPTION_JUNK):
                    seen.add(text)  # UI chrome — remember so it never reports
                    continue
                seen.add(text)
                new.append(text)
        except Exception:  # noqa: BLE001 — DOM shifts are expected
            continue
    return new


def _caption_inventory(page) -> dict[str, int]:  # noqa: ANN001
    """One-shot diagnostic: candidate-node counts per caption selector, so a
    silent run can tell 'nobody spoke' apart from 'selectors match nothing'."""
    inventory: dict[str, int] = {}
    for selector in _CAPTION_SELECTORS:
        try:
            inventory[selector] = len(page.query_selector_all(selector))
        except Exception:  # noqa: BLE001 — DOM shifts are expected
            inventory[selector] = -1
    return inventory


def pane_health(inventory: dict[str, int]) -> tuple[str, str]:
    """Pure assessment of one inventory: (state, reason).

    healthy — primary pane selector sees nodes; fallback — only broader
    selectors see nodes (primary likely renamed); quiet — no nodes anywhere
    (silence and missing pane are indistinguishable here); degraded — some
    selectors threw; dead — every selector threw (DOM shifted for sure).
    """
    if not inventory:
        return ("dead", "empty inventory — no selectors ran")
    values = list(inventory.values())
    if all(v == -1 for v in values):
        return ("dead", "all caption selectors threw — Teams DOM shifted")
    primary = inventory.get(_PRIMARY_SELECTOR, -1)
    if isinstance(primary, int) and primary > 0:
        return ("healthy", f"primary pane shows {primary} node(s)")
    nodes = sum(v for v in values if isinstance(v, int) and v > 0)
    if nodes > 0:
        return ("fallback",
                f"primary pane empty; broader selectors see {nodes} node(s)")
    if any(v == -1 for v in values):
        return ("degraded", "some caption selectors threw")
    return ("quiet", "no speech nodes — silence or missing pane")


def _dump_controls(page, tag: str) -> list[str]:  # noqa: ANN001
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


def _try_enable_captions(page) -> bool:  # noqa: ANN001
    """Enable live captions IN THE MEETING — as a TOGGLE-AWARE state machine
    (run 12 lesson): 'Show live captions' switches ON silently; a second
    click switches it back OFF. So: check already-on first, click at most ONE
    toggle per pass with a generous verify window, and let the outer retry
    re-ladder. NEVER click 'Record and transcribe' — that starts persistent
    cloud transcription/recording (consent-flagged); the owner wants only
    transient live captions for the transcript.
    Owner path (2026-09-12): More → Language and speech → Show live captions."""
    if _captions_on(page):
        logger.info("captions_already_on")
        return True
    # direct toggle on the bar (if this UI exposes one)
    for label in ("Show live captions", "Turn on live captions",
                  "Turn on captions"):
        if _click_by_text(page, label):
            return _await_captions(page, seconds=10) or _captions_on(page)
    # the More flyout path
    with contextlib.suppress(Exception):
        # ^More$ ONLY — run 8 evidence: name="More", exact=False matched the
        # app-shell sidebar's "Settings and more" gear and dumped its menu.
        page.get_by_role("button", name=re.compile(r"^(more|…)$",
                                                   re.I)).first.click(
            timeout=3000)
        page.wait_for_timeout(1200)  # flyout animation
        _dump_controls(page, "menu")
        for entry in ("Captions", "Language and speech"):
            if not _click_by_text(page, entry):
                continue
            if _captions_on(page) or _await_captions(page, seconds=4):
                return True  # older builds toggle straight from the flyout
            page.wait_for_timeout(800)
            _dump_controls(page, "captions_submenu")  # submenu evidence
            for label in ("Show live captions", "Turn on live captions",
                          "Turn on captions"):
                if _click_by_text(page, label):
                    # ONE toggle click per pass, then verify (allow silence —
                    # _captions_on also fires on the 'Hide…' label). No
                    # second click here: the outer retry re-checks
                    # already-on before touching anything again.
                    with contextlib.suppress(Exception):
                        page.keyboard.press("Escape")
                    return _await_captions(page, seconds=12) \
                        or _captions_on(page)
            with contextlib.suppress(Exception):
                page.keyboard.press("Escape")
            break  # one entry per pass; next retry starts fresh from More
        with contextlib.suppress(Exception):
            page.keyboard.press("Escape")
    logger.warning("captions_enable_failed")
    return False
