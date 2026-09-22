"""Shared Teams URL helpers (strangler extract, 2026-09-22).

Live-verified code moved verbatim from listener.py — no behavior change.
listener.py re-exports these names so existing imports keep working.
"""

import httpx
import structlog

logger = structlog.get_logger()

EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)


def _is_login_url(url: str) -> bool:
    """Microsoft login surfaces (work accounts use microsoftonline, personal
    accounts login.live.com) — popup or same-tab alike."""
    return (
        "login.microsoftonline" in url
        or "login.live.com" in url
        or "microsoftonline.com" in url
        or "account.live.com" in url
    )


def _resolve_link(url: str) -> str:
    """Follow URL shorteners (tinyurl etc.) to the real Teams meeting URL."""
    try:
        response = httpx.get(url, follow_redirects=True, timeout=20)
        resolved = str(response.url)
        if resolved != url:
            logger.info("short_link_resolved", final=resolved[:80])
        return resolved
    except httpx.HTTPError as exc:
        logger.warning("short_link_resolve_failed", error=str(exc)[:120])
        return url  # the browser may still follow it


def _direct_meeting_url(launcher_url: str) -> str:
    """Skip the launcher entirely: the /dl/launcher page encodes the real
    meeting URL in its ?url= parameter ("/_#/meet/<id>?p=<passcode>")."""
    from urllib.parse import parse_qs, unquote, urlparse

    parsed = urlparse(launcher_url)
    inner = parse_qs(parsed.query).get("url", [None])[0]
    if not inner:
        return launcher_url
    decoded = unquote(inner)
    if not decoded.startswith("/"):
        return launcher_url
    direct = f"{parsed.scheme}://{parsed.netloc}{decoded}"
    if "anon=" not in decoded and "teams.live" in parsed.netloc:
        direct += "&anon=true"
    logger.info("direct_meeting_url", url=direct[:90])
    return direct
