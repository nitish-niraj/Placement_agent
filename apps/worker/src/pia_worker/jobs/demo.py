"""Demo jobs proving the F-003 acceptance behavior:

- `flaky_until_third_attempt` fails twice, succeeds on the 3rd attempt (retry path).
- `always_fails` exhausts retries and lands in the dead-letter queue.

Pure helpers (`_should_fail`) are unit-testable without Redis; the end-to-end demo
runs against the compose stack (tests/integration/test_queue_smoke.py).
"""

import datetime as dt
import uuid
from typing import Any

import redis as redis_lib
from rq import get_current_job

HEARTBEAT_KEY = "pia:worker:heartbeat"


def _should_fail(attempt: int, succeed_on: int = 3) -> bool:
    """True while `attempt` is below `succeed_on` (attempts are 1-based)."""
    return attempt < succeed_on


def flaky_until_third_attempt(a: int, b: int, redis_url: str) -> int:
    job = get_current_job()
    if job is None:  # pragma: no cover - only when called outside RQ
        raise RuntimeError("flaky job must run inside RQ")
    client = redis_lib.Redis.from_url(redis_url)
    attempts = client.incr(f"pia:demo:{job.id}:attempts")
    client.expire(f"pia:demo:{job.id}:attempts", 3600)
    if _should_fail(attempts):
        raise RuntimeError(f"transient failure #{attempts} (job {job.id})")
    return a + b


def always_fails() -> None:
    raise RuntimeError("permanent failure — should dead-letter (F-003 DLQ demo)")


def touch_heartbeat(redis_url: str) -> str:
    """One-shot heartbeat writer; the worker loop also sets this periodically."""
    client = redis_lib.Redis.from_url(redis_url)
    now = dt.datetime.now(dt.UTC).isoformat()
    client.set(HEARTBEAT_KEY, now)
    return now


def record_job_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    """Placeholder fan-out target used by tests to verify enqueue/consume round-trips."""
    return payload


def new_correlation_id() -> str:
    return str(uuid.uuid4())
