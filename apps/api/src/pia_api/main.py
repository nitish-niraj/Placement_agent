"""FastAPI application factory."""

import logging
from pathlib import Path

import structlog
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from pia_api import __version__
from pia_api.deps import require_dashboard_token
from pia_api.routes import dashboard, documents, health, profile, webhooks
from pia_api.settings import get_settings


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging()

    app = FastAPI(
        title="Placement Intelligence Agent API",
        version=__version__,
        docs_url="/docs" if settings.environment == "development" else None,
    )

    # Health stays unauthenticated (uptime monitors / compose healthchecks).
    app.include_router(health.router)
    # Webhooks authenticate via their own shared-secret header (SEC-003), not the
    # dashboard token — Evolution API is the caller.
    app.include_router(webhooks.router)

    # Protected surface — every dashboard/business route mounts behind the token.
    app.include_router(profile.router)
    app.include_router(documents.router)
    app.include_router(dashboard.router)
    protected = APIRoute("/api/v1/ping", endpoint=_ping, methods=["GET"], tags=["meta"],
                         dependencies=[Depends(require_dashboard_token)])
    app.router.routes.append(protected)

    # P11: serve the built React dashboard when present (hash-routed SPA, so a
    # static mount with html=True is sufficient; API routes keep precedence).
    for candidate in (
        Path("/app/dashboard-dist"),
        Path(__file__).resolve().parents[3] / "dashboard" / "dist",
    ):
        if (candidate / "index.html").is_file():
            app.mount("/", StaticFiles(directory=str(candidate), html=True),
                      name="dashboard")
            break

    return app


def _ping() -> dict:
    return {"pong": True, "service": "pia-api", "version": __version__}


app = create_app()
