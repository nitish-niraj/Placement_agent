"""Worker entrypoint: heartbeat thread + RQ worker over the default queue.

Run locally:  python -m pia_worker.main
Run in docker: CMD of apps/worker/Dockerfile.
"""

import datetime as dt
import threading
import time

import redis as redis_lib
import structlog
from rq import Queue, SimpleWorker

from pia_worker.jobs.demo import HEARTBEAT_KEY
from pia_worker.queue import DEFAULT_QUEUE, MAINTENANCE_QUEUE, make_connection, make_queue
from pia_worker.settings import get_settings

logger = structlog.get_logger()


def _heartbeat_loop(client: redis_lib.Redis, interval_seconds: int) -> None:
    """Silence must be detected, not assumed (DEC-003 / TRD §10.4)."""
    ttl = max(interval_seconds * 3, 30)
    while True:
        try:
            client.setex(HEARTBEAT_KEY, ttl, dt.datetime.now(dt.UTC).isoformat())
        except Exception:  # noqa: BLE001 — heartbeat must never crash the worker
            logger.warning("heartbeat_write_failed")
        time.sleep(interval_seconds)


def _maintenance_loop(client: redis_lib.Redis, interval_hours: int) -> None:
    """Enqueue the retention job at startup, then daily (SEC-007)."""
    queue = Queue(MAINTENANCE_QUEUE, connection=client)
    interval = max(interval_hours * 3600, 60)
    while True:
        try:
            queue.enqueue("pia_worker.jobs.process_message.retention_cleanup")
        except Exception:  # noqa: BLE001 — scheduler must never crash the worker
            logger.warning("maintenance_enqueue_failed")
        time.sleep(interval)


def _deadline_sweep_loop(client: redis_lib.Redis, interval_minutes: int) -> None:
    """F-020: hourly deadline state sweep (OPEN -> DUE_SOON -> EXPIRED)."""
    queue = Queue(MAINTENANCE_QUEUE, connection=client)
    interval = max(interval_minutes * 60, 60)
    while True:
        try:
            queue.enqueue("pia_worker.jobs.extract_events.deadline_state_sweep")
        except Exception:  # noqa: BLE001 — scheduler must never crash the worker
            logger.warning("deadline_sweep_enqueue_failed")
        time.sleep(interval)


def _digest_loop(client: redis_lib.Redis, hour: int, minute: int) -> None:
    """F-025: enqueue the daily digest at the configured local time (default
    20:30 Asia/Kolkata; TRD §8)."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    from pia_worker.settings import get_settings

    queue = Queue(MAINTENANCE_QUEUE, connection=client)
    tz = ZoneInfo(get_settings().app_timezone)
    while True:
        now = dt.datetime.now(tz=tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info("digest_scheduled", at=target.isoformat())
        time.sleep(wait_seconds)
        try:
            queue.enqueue("pia_worker.jobs.notify.daily_digest")
        except Exception:  # noqa: BLE001 — scheduler must never crash the worker
            logger.warning("digest_enqueue_failed")


def main() -> None:
    settings = get_settings()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),
    )

    connection = make_connection(settings.redis_url)
    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(connection, settings.heartbeat_interval_seconds),
        daemon=True,
        name="pia-heartbeat",
    )
    thread.start()

    maintenance = threading.Thread(
        target=_maintenance_loop,
        args=(connection, settings.maintenance_interval_hours),
        daemon=True,
        name="pia-maintenance",
    )
    maintenance.start()

    sweep = threading.Thread(
        target=_deadline_sweep_loop,
        args=(connection, settings.deadline_sweep_interval_minutes),
        daemon=True,
        name="pia-deadline-sweep",
    )
    sweep.start()

    digest = threading.Thread(
        target=_digest_loop,
        args=(connection, settings.digest_hour, settings.digest_minute),
        daemon=True,
        name="pia-digest",
    )
    digest.start()

    # Self-healing worker loop: a transient Redis blip must never leave the
    # pipeline dead (chaos-test requirement: "worker restart" §20; NFR-001).
    while True:
        try:
            connection = make_connection(settings.redis_url)
            default_queue = make_queue(DEFAULT_QUEUE, connection=connection)
            maintenance_queue = make_queue(MAINTENANCE_QUEUE, connection=connection)
            logger.info("worker_started", environment=settings.environment)
            worker = SimpleWorker([default_queue, maintenance_queue], connection=connection)
            worker.work(with_scheduler=False)
            logger.warning("worker_loop_returned")  # work() only returns on shutdown
        except Exception as exc:  # noqa: BLE001 — logged, then retried below
            logger.error("worker_crashed", error=str(exc))
        logger.warning("worker_restarting", backoff_seconds=5)
        time.sleep(5)


if __name__ == "__main__":
    main()
