"""Job enqueueing from the API process.

Jobs are referenced by import string so the API never imports worker code —
only the worker container resolves them (keeps the dependency direction clean).
"""

from redis import Redis
from rq import Queue, Retry

from pia_api.settings import get_settings

DEFAULT_QUEUE = "default"


def _queue() -> Queue:
    settings = get_settings()
    return Queue(DEFAULT_QUEUE, connection=Redis.from_url(settings.redis_url))


def enqueue_process_message(message_id: str, correlation_id: str = "") -> None:
    _queue().enqueue(
        "pia_worker.jobs.process_message.process_message",
        message_id,
        correlation_id,
        retry=Retry(max=3),
    )


def enqueue_download_attachment(attachment_id: str) -> None:
    _queue().enqueue(
        "pia_worker.jobs.process_message.download_attachment",
        attachment_id,
        retry=Retry(max=5),
    )
