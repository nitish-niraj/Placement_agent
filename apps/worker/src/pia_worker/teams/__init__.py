"""Teams personal listener (DEC-008 amendment): Type-1 informational KYC only."""


def is_teams_url(url: str) -> bool:
    """Teams web lives on several domains: teams.microsoft.com (classic),
    teams.cloud.microsoft (new-domain migration), teams.live.com (personal)."""
    u = url or ""
    return ("teams.microsoft" in u or "teams.cloud.microsoft" in u
            or "teams.live" in u)
