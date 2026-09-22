"""Form-provider detection shared by the API pre-fill endpoint and the worker
propose path, so both sides agree on what counts as a form.

Providers and their final-URL shapes (verified shapes only — new shapes are
added with a live specimen, never guessed):
- google:    docs.google.com/forms/... (full entry-ID pre-fill supported)
- microsoft: forms.office.com/r/<id> or .../Pages/ResponsePage.aspx?id=<id>
  (manual path: Microsoft's prefill links are owner-generated per form, so a
  third party cannot mint entry params without a specimen to parse)
- glide:     *glide* hosts (manual path: no generic prefill scheme exists)
- teams:     meeting launchers — never a form, refused everywhere
- unknown:   anything else — refused (safe default)
"""

GOOGLE = "google"
MICROSOFT = "microsoft"
GLIDE = "glide"
TEAMS = "teams"
UNKNOWN = "unknown"

_MICROSOFT_HOSTS = frozenset({"forms.office.com"})


def detect_form_provider(final_url: str) -> str:
    """Classify a resolved (post-redirect) URL. Pure string logic, no fetch."""
    import urllib.parse

    try:
        parsed = urllib.parse.urlparse(final_url or "")
    except Exception:  # noqa: BLE001 — unparseable means unknown
        return UNKNOWN
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host == "docs.google.com" and path.startswith("/forms/"):
        return GOOGLE
    if host in _MICROSOFT_HOSTS and (
            path.startswith("/r/") or path.startswith("/Pages/")):
        return MICROSOFT
    if "glide" in host and path not in ("", "/"):
        return GLIDE
    if "teams.microsoft.com" in host or host.endswith("teams.live.com"):
        return TEAMS
    return UNKNOWN
