"""P14.1 Google Forms driver (Playwright, headless chromium).

Google Forms DOM is stable enough for role-based selectors, but its classes
are obfuscated — everything here uses roles and structure, and every selector
miss degrades loudly (skipped question + diagnostic screenshot), never a crash
or a wrong submit. The first REAL form tunes these selectors — same
evidence-based approach the Teams listener used. Multi-page forms are detected
and refused to manual handling rather than half-filled."""

import re

import structlog

from pia_worker.automation.fields import Question, pick_option

logger = structlog.get_logger()


class FormDriverError(RuntimeError):
    """The form could not be driven (structure unexpected, multi-page, ...)."""


_WALL_NOTE = ("Google sign-in required — this form is restricted to signed-in "
              "accounts and the robot has no Google session. "
              "Fill it manually while signed in.")


def login_wall_note(page) -> str | None:
    """Detect a Google sign-in wall (org-restricted form): the headless
    browser carries no Google session, so such forms render zero questions.
    Returns a human-readable note for the Telegram report, or None when the
    page looks like a real form. Never raises — detection must not fail a run
    (spacing-tolerant: the sign-in DOM concatenates words without spaces)."""
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001 — detached/closed page
        return None
    if "accounts.google.com" in url:
        return _WALL_NOTE
    try:
        title = (page.title() or "").lower()
        body = (page.locator("body").text_content() or "").lower()
    except Exception:  # noqa: BLE001 — DOM unreadable, let read_questions decide
        return None
    if "google forms" in title and "sign-in" in title:
        return _WALL_NOTE
    compact = re.sub(r"[^a-z0-9]+", "", body)
    if "signintocontinuetogoogleforms" in compact:
        return _WALL_NOTE
    return None


def read_questions(page) -> list[Question]:
    """Every question on the page: text (star stripped), kind, required flag,
    and its global listitem position (fill uses the same index later)."""
    questions: list[Question] = []
    items = page.locator('div[role="listitem"]')
    for position in range(items.count()):
        item = items.nth(position)
        try:
            heading = item.locator('[role="heading"]').first
            raw = (heading.text_content() or "").strip()
        except Exception:  # noqa: BLE001 — DOM shifts are expected
            raw = ""
        if not raw:
            continue  # not a question (section header, grid decoration, ...)
        required = raw.endswith("*")
        text = raw.rstrip("*").strip()
        if item.locator('div[role="listbox"]').count():
            kind = "dropdown"
        elif item.locator('div[role="radio"]').count():
            kind = "radio"
        elif item.locator('div[role="checkbox"]').count():
            kind = "checkbox"
        elif item.locator("textarea").count():
            kind = "long_text"
        elif item.locator('input[type="email"]').count():
            kind = "email"
        elif item.locator('input[type="text"]').count():
            kind = "text"
        elif item.locator('input[type="date"]').count():
            kind = "date"
        else:
            kind = "other"
        options: tuple[str, ...] = ()
        if kind in ("radio", "checkbox"):
            controls = item.locator(f'div[role="{kind}"]')
            texts = []
            for i in range(controls.count()):
                control = controls.nth(i)
                label = (control.get_attribute("aria-label")
                         or control.text_content() or "").strip()
                if label:
                    texts.append(label)
            options = tuple(texts)
        questions.append(Question(index=position, text=text, kind=kind,
                                  required=required, options=options))
    return questions


def fill_question(page, question: Question, value: str) -> bool:
    """Fill one question. Dropdowns are clicked open and the value is fuzzy-
    matched against the options. Returns False when the form wouldn't accept
    the value (e.g. the teacher name is not among the options) — reported,
    never guessed."""
    item = page.locator('div[role="listitem"]').nth(question.index)
    if question.kind in ("text", "email"):
        item.locator('input[type="text"], input[type="email"]').first.fill(value)
        return True
    if question.kind == "long_text":
        item.locator("textarea").first.fill(value)
        return True
    if question.kind == "date":
        item.locator('input[type="date"]').first.fill(value)
        return True
    if question.kind == "dropdown":
        item.locator('div[role="listbox"]').first.click()
        page.wait_for_selector('div[role="option"]', timeout=5000)
        visible = [o for o in page.locator('div[role="option"]').all()
                   if o.is_visible()]
        texts = [(o.text_content() or "").strip() for o in visible]
        chosen = pick_option(texts, value)
        if chosen is None:
            page.keyboard.press("Escape")
            logger.warning("dropdown_option_not_found", question=question.text[:60],
                           value=value[:40], options=texts[:5])
            return False
        visible[texts.index(chosen)].click()
        return True
    if question.kind in ("radio", "checkbox"):
        controls = item.locator(f'div[role="{question.kind}"]')
        labels: list[str] = []
        for i in range(controls.count()):
            control = controls.nth(i)
            labels.append((control.get_attribute("aria-label")
                           or control.text_content() or "").strip())
        chosen = pick_option(labels, value)
        if chosen is None:
            return False
        controls.nth(labels.index(chosen)).click()
        return True
    return False


def submit(page) -> bool:
    """Click Submit and confirm the response was recorded. Raises
    FormDriverError on a multi-page form (Next without Submit) — those are
    refused to manual handling rather than half-filled."""
    button = page.get_by_role("button",
                              name=re.compile(r"^\s*(submit|send)\b", re.I))
    if button.count() == 0:
        nxt = page.get_by_role("button", name=re.compile(r"^\s*next\b", re.I))
        if nxt.count():
            raise FormDriverError("multi-page form — not supported, fill manually")
        raise FormDriverError("submit button not found — form structure unexpected")
    button.first.click(timeout=5000)
    try:
        page.wait_for_url("**/formResponse", timeout=10000)
        return True
    except Exception:  # noqa: BLE001 — some forms confirm by text only
        content = (page.locator("body").text_content() or "").lower()
        return "recorded" in content or "response has been" in content
