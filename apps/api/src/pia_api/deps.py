"""Auth dependencies.

Dashboard/API routes require `Authorization: Bearer <DASHBOARD_TOKEN>` (SEC-002 posture,
single-user deployment). Health endpoints stay unauthenticated so the VPS healthcheck and
uptime monitors can call them without a secret.

NOTE: dependency parameters must NOT use pydantic-model annotations — FastAPI would
turn them into request-body fields and corrupt every route's body schema.
"""

from fastapi import Header, HTTPException, status

from pia_api.settings import get_settings


def require_dashboard_token(
    authorization: str | None = Header(default=None),
) -> None:
    settings = get_settings()
    if not settings.require_auth:
        return
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.dashboard_token or token == "change-me":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
