"""Inline pre-fill links for form_draft approvals (owner decision 2026-09-21).

The owner reviews and submits every form in their own browser: the dashboard
expands a draft into an embedded Google Form whose fields arrive pre-filled
from the stored profile, and the owner clicks Submit themselves. The server
never fills or submits anything — the headless P14.1 executor is retired from
the approve path (manual Submit only, per owner decision).

Mechanism: Google Forms accepts ``?entry.<id>=<value>`` pre-fill parameters.
Entry IDs are extracted server-side from the form's ``FB_PUBLIC_LOAD_DATA_``
blob (read-only GET, SSRF-guarded to docs.google.com), mapped with the same
deterministic hint catalog the executor used, and choice values must match an
option exactly (case-insensitive) or the field is left blank for the owner.
Judgement questions ("Registered on ...?") have no safe value and are always
left blank. The pre-filled URL is never logged or persisted (values are PII).
"""

import json
import re
import urllib.parse
from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger()

# Mirror of the worker's deterministic hint catalog (apps/worker/.../fields.py
# _HINTS): API must not import worker code, so the order and patterns are
# duplicated here. Specific fields BEFORE the generic "name" rule.
_HINTS: list[tuple[str, re.Pattern[str]]] = [
    ("teacher", re.compile(
        r"\b(teacher|faculty|presenter|professor|speaker|mentor|"
        r"conducted by|taken by|presented by|session was taken)\b", re.IGNORECASE)),
    ("company", re.compile(
        r"\b(compan(?:y|ies)|organization|organisation|firm|recruiter|"
        r"which company)\b", re.IGNORECASE)),
    ("registration_number",
     re.compile(r"\bregistration\b|\bregn\.?\b|\breg\.?\s*no", re.IGNORECASE)),
    ("roll_number", re.compile(r"\broll\b", re.IGNORECASE)),
    ("student_id", re.compile(r"\b(student\s*id|scholar\s*(no|number)|uid)\b", re.IGNORECASE)),
    ("email", re.compile(r"\be-?mail\b", re.IGNORECASE)),
    ("mobile", re.compile(r"\b(mobile|phone|contact\s*(no|number)|tel\.?)\b",
                          re.IGNORECASE)),
    ("backlog_count", re.compile(r"\b(backlogs?|arrears?)\b", re.IGNORECASE)),
    ("cgpa", re.compile(r"\b(cgpa|sgpa)\b", re.IGNORECASE)),
    ("tenth_percent", re.compile(r"\b(10th|x(th)?\s*class|matric)\b", re.IGNORECASE)),
    ("twelfth_percent", re.compile(r"\b(12th|xii(th)?\s*class|intermediate)\b", re.IGNORECASE)),
    ("branch", re.compile(
        r"\b(course|branch|programme|program|stream|specialization)\b", re.IGNORECASE)),
    ("batch", re.compile(r"\bbatch\b", re.IGNORECASE)),
    ("full_name", re.compile(r"\bname\b", re.IGNORECASE)),
]

_MAX_HTML_BYTES = 3_000_000
_TIMEOUT_S = 15.0


class PrefillError(RuntimeError):
    """The pre-fill link could not be built (unresolvable/short link, fetch
    failure, not a Google Form, no parseable questions). Carries the HTTP
    status the route should answer with."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class FormEntry:
    """One form question: the response field id (``entry.<id>`` — the item id
    for text fields, the nested sub-id for choice fields), question text, the
    kind (``text`` | ``choice`` | ``other``), and candidate option labels."""

    entry_id: str
    text: str
    kind: str
    options: tuple[str, ...]


# Google FB item-type ints (4th element of the item array), verified against a
# real form: 0 = short/paragraph text, 2 = radio, 3 = checkbox, 4 = dropdown.
_TEXT_TYPES = frozenset({0, 1})
_CHOICE_TYPES = frozenset({2, 3, 4})


def resolve_form_url(target: str) -> str:
    """Follow short links to the canonical form URL. SSRF-guarded: http(s)
    only, bounded redirects; the final host must be a known form provider
    (Google/Microsoft/Glide) — a short link may point anywhere."""
    from pia_shared.formlinks import (
        GLIDE,
        GOOGLE,
        MICROSOFT,
        TEAMS,
        detect_form_provider,
    )

    parsed = urllib.parse.urlparse((target or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PrefillError("draft link is not a fetchable http(s) URL", 422)
    try:
        with httpx.Client(follow_redirects=True, max_redirects=5,
                          timeout=_TIMEOUT_S) as client:
            response = client.get(target)
    except httpx.HTTPError as exc:
        raise PrefillError(f"could not open the form link: {exc!s}"[:160]) from exc
    final = str(response.url)
    provider = detect_form_provider(final)
    if provider == GOOGLE:
        pass
    elif provider in (MICROSOFT, GLIDE):
        logger.info("form_provider_manual", provider=provider,
                    target=str(target)[:80])
    elif provider == TEAMS:
        raise PrefillError("this link opens a Teams meeting, not a form — "
                           "there is nothing to pre-fill", 422)
    else:
        raise PrefillError("link does not resolve to a supported form "
                           "(Google/Microsoft/Glide) — open it manually", 422)
    if response.status_code >= 400 and response.status_code not in (401, 403):
        raise PrefillError(f"form page returned HTTP {response.status_code}")
    return final


def fetch_form_html(url: str) -> str:
    """Read-only GET of the form page (no session, no interaction). A 401/403
    means Google demands a signed-in session even to read (e.g. edit links) —
    that returns "" so the draft reports as manual instead of erroring: the
    owner opens it under their own login."""
    try:
        with httpx.Client(follow_redirects=True, max_redirects=5,
                          timeout=_TIMEOUT_S) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise PrefillError(f"could not read the form page: {exc!s}"[:160]) from exc
    if response.status_code in (401, 403):
        return ""
    if response.status_code >= 400:
        raise PrefillError(f"form page returned HTTP {response.status_code}")
    if len(response.content) > _MAX_HTML_BYTES:
        raise PrefillError("form page unexpectedly large — open it manually")
    return response.text


def _item_extent(blob: str, start: int) -> str:
    """Bracket-scan the FB data array starting at `start` (which points at
    `[`), honouring double-quoted strings with backslash escapes."""
    depth, i, n = 0, start, len(blob)
    in_string, escape = False, False
    while i < n:
        char = blob[i]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return blob[start:i + 1]
        i += 1
    return blob[start:]


def _split_top_level(array: str) -> list[str]:
    """Split a ``[...]`` array literal into its top-level element substrings,
    honouring nesting and double-quoted strings with backslash escapes."""
    inner = array.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    parts, depth, current = [], 0, []
    in_string, escape = False, False
    for char in inner:
        if in_string:
            current.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            current.append(char)
        elif char == "[":
            depth += 1
            current.append(char)
        elif char == "]":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _payload_details(payload: str) -> tuple[str | None, tuple[str, ...]]:
    """Item payload (5th element) → (response field id, option labels).

    The response field is the first nested ``[[<id>...`` — for text items
    ``[[440445981,null,1]]``, for choice items ``[[488462340,[["Yes",...]]]]``
    (verified live: ``entry.<that id>`` is what Google pre-fills and submits).
    Option labels are the label-first ``["Yes",null,...]`` rows; the question
    title never lives in the payload."""
    id_match = re.search(r"\[\[(\d{7,})", payload)
    response_id = id_match.group(1) if id_match else None
    labels = [_unescape(m.group(1)).strip()
              for m in re.finditer(r'\["((?:[^"\\]|\\.)+)",null', payload)]
    options = tuple(dict.fromkeys(
        label for label in labels if label and len(label) <= 80))[:25]
    return response_id, options


def _unescape(raw: str) -> str:
    """Decode quote, backslash and unicode escapes without touching real
    unicode (unlike unicode_escape, which mangles non-Latin text).
    Falls back to raw."""
    try:
        return json.loads('"' + raw + '"')
    except (json.JSONDecodeError, ValueError):
        return raw


def extract_entries(html: str) -> list[FormEntry]:
    """Question text → entry ID (+ candidate option labels) from the form's
    ``FB_PUBLIC_LOAD_DATA_`` blob. Returns [] when nothing parseable is found
    (login-walled pages carry no data blob — reported as manual downstream)."""
    marker = html.find("FB_PUBLIC_LOAD_DATA_")
    if marker < 0:
        return []
    blob_start = html.find("[", marker)
    # Scan only the data-blob extent: stray [123,"..."] arrays elsewhere on
    # the page (analytics, config) must not become phantom questions.
    blob = _item_extent(html, blob_start) if blob_start >= 0 else ""
    if not blob:
        return []
    entries: list[FormEntry] = []
    item_re = re.compile(r'\[(\d{7,}),"((?:[^"\\]|\\.)+)"')
    for match in item_re.finditer(blob):
        item_id, raw_title = match.group(1), match.group(2)
        title = _unescape(raw_title).strip()
        if not title or len(title) > 200:
            continue
        extent = _item_extent(blob, match.start())
        elements = _split_top_level(extent)
        item_type = int(elements[3]) if len(elements) > 3 and elements[3].strip().isdigit() else -1
        payload = elements[4] if len(elements) > 4 else ""
        if item_type in _TEXT_TYPES:
            response_id, _ = _payload_details(payload)
            entry = FormEntry(entry_id=response_id or item_id, text=title,
                              kind="text", options=())
        elif item_type in _CHOICE_TYPES:
            response_id, options = _payload_details(payload)
            entry = FormEntry(entry_id=response_id or item_id, text=title,
                              kind="choice", options=options)
        else:
            entry = FormEntry(entry_id=item_id, text=title, kind="other", options=())
        if any(e.entry_id == entry.entry_id for e in entries):
            continue  # same response field echoed later in the blob
        entries.append(entry)
    return entries


def map_values(entries: list[FormEntry], bag: dict[str, str]
               ) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Entry ID → value for pre-fill, plus left-blank reports. Conservative:
    a hint must match AND the bag must hold the value; choice fields
    additionally require an exact (case-insensitive) option match; unknown
    question types are always left blank. Never guessed."""
    filled: dict[str, str] = {}
    left_blank: list[dict[str, str]] = []
    for entry in entries:
        if entry.kind == "other":
            left_blank.append({"question": entry.text[:80],
                               "reason": "unsupported question type — fill manually"})
            continue
        key = next((k for k, pattern in _HINTS if pattern.search(entry.text)), None)
        if key is None:
            left_blank.append({"question": entry.text[:80], "reason": "no matching profile field"})
            continue
        value = (bag.get(key) or "").strip()
        if not value:
            left_blank.append({"question": entry.text[:80], "reason": "no value on file"})
            continue
        if entry.kind == "choice":
            exact = next((o for o in entry.options
                          if o.strip().casefold() == value.casefold()), None)
            if exact is None:
                left_blank.append({"question": entry.text[:80],
                                   "reason": "answer it yourself"})
                continue
            filled[entry.entry_id] = exact
        else:
            filled[entry.entry_id] = value
    return filled, left_blank


def build_prefill_url(base_url: str, filled: dict[str, str]) -> str:
    """Canonical form URL + entry params + Google's pre-fill marker
    (``usp=pp_url``) + embedded mode (iframe-ready; the same URL works in a
    new tab). Existing query is preserved; stale entry params are replaced."""
    parts = urllib.parse.urlsplit(base_url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in query
             if not k.startswith("entry.") and k not in ("usp", "embedded")]
    query.extend((f"entry.{entry_id}", value) for entry_id, value in filled.items())
    query.append(("usp", "pp_url"))
    query.append(("embedded", "true"))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path,
         urllib.parse.urlencode(query), parts.fragment))


def _without_edit_requested(url: str) -> str:
    """Drafts are always fresh fills: an `edit_requested=true` link renders a
    signed-in user an interstitial splash ("Fill out form" button — an extra
    click, sometimes a new tab) instead of the questions. The plain viewform
    is the fillable form, so drop that param everywhere we build links."""
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in
             urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k != "edit_requested"]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path,
         urllib.parse.urlencode(query), parts.fragment))


def prefill_for_target(target: str, bag: dict[str, str]) -> dict:
    """Full pipeline for one draft link. Returns a JSON-safe report (the URL
    itself is PII-bearing — callers must not log it). Google forms get the
    entry-ID pipeline; Microsoft/Glide get a guided manual report (their
    prefill schemes need a live specimen to parse — the tripwire log marks
    its arrival); zero entries (e.g. a login-walled form) is a report, not
    an error: the owner opens it manually (iframe shows Google sign-in under
    their own session)."""
    from pia_shared.formlinks import GOOGLE, detect_form_provider

    canonical = _without_edit_requested(resolve_form_url(target))
    provider = detect_form_provider(canonical)
    if provider != GOOGLE:
        names = {"microsoft": "Microsoft", "glide": "Glide"}
        label = names.get(provider, "this")
        return {"prefill_url": canonical, "source_url": canonical,
                "provider": provider, "filled": [], "left_blank": [],
                "note": f"{label} form — the portal can't pre-fill these yet: "
                        "it opens below for you to fill (your login works "
                        "there); text fields can also use the auto-fill bookmark."}
    entries = extract_entries(fetch_form_html(canonical))
    if not entries:
        return {"prefill_url": build_prefill_url(canonical, {}),
                "source_url": canonical, "provider": GOOGLE,
                "filled": [], "left_blank": [],
                "note": "no readable questions (sign-in wall?) — sign in inside "
                        "the form below and fill manually"}
    filled, left_blank = map_values(entries, bag)
    return {"prefill_url": build_prefill_url(canonical, filled),
            "source_url": canonical, "provider": GOOGLE,
            "filled": [{"question": e.text[:80]} for e in entries
                       if e.entry_id in filled],
            "left_blank": left_blank, "note": ""}
