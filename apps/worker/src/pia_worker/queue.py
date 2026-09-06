"""Thin queue abstraction over RQ (F-003).

Retries use exponential-friendly RQ `Retry`; jobs that exhaust their retries land in
RQ's FailedJobRegistry, which the DLQ view treats as the dead-letter queue (§14).
"""

from collections.abc import Callable
from typing import Any

import redis as redis_lib
from rq import Queue, Retry

from pia_worker.settings import get_settings

DEFAULT_QUEUE = "default"
MAINTENANCE_QUEUE = "maintenance"


def make_connection(redis_url: str | None = None) -> redis_lib.Redis:
    url = redis_url or get_settings().redis_url
    # health_check_interval keeps long-lived blocking connections self-verifying
    return redis_lib.Redis.from_url(url, socket_connect_timeout=5, health_check_interval=30)


def make_queue(name: str = DEFAULT_QUEUE, connection: redis_lib.Redis | None = None) -> Queue:
    return Queue(name, connection=connection or make_connection())


def enqueue_job(
    queue: Queue,
    func: Callable[..., Any],
    *args: Any,
    retries: int = 0,
    **kwargs: Any,
) -> Any:
    retry = Retry(max=retries) if retries > 0 else None
    return queue.enqueue(func, *args, retry=retry, **kwargs)
