"""Deploy-time secret validation (SEC-001 hardening).

A bad `.env` must fail loudly at boot, never run silently insecure: placeholder
or missing dashboard tokens are refused in every environment, and plaintext
PII mode (empty PIA_ENCRYPTION_KEY, SEC-002) is refused outside development.
Pure function so unit tests cover it without booting anything."""

PLACEHOLDER_TOKENS = frozenset({"", "change-me"})


def find_secret_problems(*, dashboard_token: str | None, environment: str,
                         pia_encryption_key: str) -> list[str]:
    """Human-readable problems; empty means safe to boot. Pass
    dashboard_token=None when the process serves no dashboard (worker)."""
    problems: list[str] = []
    if dashboard_token is not None and dashboard_token.strip() in PLACEHOLDER_TOKENS:
        problems.append("DASHBOARD_TOKEN is missing or still the placeholder")
    if (environment or "development") != "development" and not (
            pia_encryption_key or "").strip():
        problems.append("PIA_ENCRYPTION_KEY is empty outside development "
                        "(SEC-002 forbids plaintext PII mode)")
    return problems
