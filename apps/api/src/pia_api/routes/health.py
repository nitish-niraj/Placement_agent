"""Health composition — 02_TRD §10.4.

POST /webhooks/evolution arrives in P1; until then `evolution_api` reports
`not_configured`, which is informational and does not degrade overall status.
"""

import datetime as dt
import os

import httpx
import redis as redis_lib
import sqlalchemy
from fastapi import APIRouter
from sqlalchemy import text

from pia_api.settings import get_settings

router = APIRouter(tags=["health"])

WORKER_HEARTBEAT_KEY = "pia:worker:heartbeat"
WORKER_STALE_SECONDS = 90

_CORE_CHECKS = {"postgres", "redis"}


def aggregate_status(checks: dict[str, str]) -> str:
    """Overall status: core stores must be ok; anything degraded -> degraded; else ok."""
    for name in _CORE_CHECKS:
        if checks.get(name) != "ok":
            return "down"
    values = set(checks.values())
    if "down" in values:
        return "down"
    if "degraded" in values:
        return "degraded"
    return "ok"


def _check_postgres(database_url: str) -> str:
    try:
        engine = sqlalchemy.create_engine(database_url, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return "down"
    return "ok"


def _check_redis(redis_url: str) -> tuple[str, int, str]:
    try:
        client = redis_lib.Redis.from_url(redis_url, socket_connect_timeout=2)
        client.ping()
        queue_depth = client.llen("rq:queue:default")
        heartbeat = client.get(WORKER_HEARTBEAT_KEY)
    except Exception:
        return "down", 0, "unknown"
    if isinstance(heartbeat, bytes):
        heartbeat = heartbeat.decode()
    return "ok", queue_depth, heartbeat or ""


def _check_minio(endpoint: str) -> str:
    try:
        response = httpx.get(f"{endpoint.rstrip('/')}/minio/health/live", timeout=2.0)
    except Exception:
        return "down"
    return "ok" if response.status_code == 200 else "degraded"


def _check_evolution(base_url: str) -> str:
    if not base_url:
        return "not_configured"  # expected until P1
    try:
        response = httpx.get(base_url.rstrip("/"), timeout=2.0)
    except Exception:
        return "down"
    return "ok" if response.status_code < 500 else "degraded"


def _check_worker(heartbeat: str) -> str:
    if not heartbeat:
        return "degraded"  # silence is surfaced, never assumed benign (DEC-003)
    try:
        age = (dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(heartbeat)).total_seconds()
    except ValueError:
        return "degraded"
    return "ok" if age <= WORKER_STALE_SECONDS else "degraded"


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    redis_status, queue_depth, heartbeat = _check_redis(settings.redis_url)

    checks: dict[str, str] = {
        "api": "ok",
        "postgres": _check_postgres(settings.database_url),
        "redis": redis_status,
        "minio": _check_minio(settings.minio_endpoint),
        "evolution_api": _check_evolution(settings.evolution_base_url),
        "worker": _check_worker(heartbeat) if redis_status == "ok" else "unknown",
    }
    return {
        "status": aggregate_status(checks),
        "checks": checks,
        "meta": {
            "version": settings.app_version,
            "environment": settings.environment,
            "timezone": settings.app_timezone,
            "queue_depth": queue_depth,
            "pid": os.getpid(),
            "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        },
    }
