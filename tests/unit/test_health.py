"""Health aggregation logic (TRD §10.4)."""

from pia_api.routes.health import aggregate_status


def test_all_ok_is_ok() -> None:
    checks = {
        "api": "ok", "postgres": "ok", "redis": "ok",
        "minio": "ok", "evolution_api": "not_configured", "worker": "ok",
    }
    assert aggregate_status(checks) == "ok"


def test_core_store_down_means_down() -> None:
    assert aggregate_status({"postgres": "down", "redis": "ok"}) == "down"
    assert aggregate_status({"postgres": "ok", "redis": "down"}) == "down"


def test_any_degraded_is_degraded() -> None:
    checks = {"postgres": "ok", "redis": "ok", "minio": "ok", "worker": "degraded"}
    assert aggregate_status(checks) == "degraded"


def test_any_down_is_down() -> None:
    checks = {"postgres": "ok", "redis": "ok", "minio": "down", "worker": "ok"}
    assert aggregate_status(checks) == "down"


def test_worker_degraded_when_silent() -> None:
    """Silence is detected, not assumed (DEC-003)."""
    checks = {"postgres": "ok", "redis": "ok", "worker": "degraded"}
    assert aggregate_status(checks) == "degraded"
