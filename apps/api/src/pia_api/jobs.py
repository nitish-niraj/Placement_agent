"""Job enqueueing from the API process.

Jobs are referenced by import string so the API never imports worker code —
only the worker container resolves them (keeps the dependency direction clean).
"""

from redis import Redis
from rq import Queue, Retry

from pia_api.settings import get_settings
from pia_shared.queues import DEFAULT_QUEUE, REALTIME_QUEUE


def _queue(name: str = DEFAULT_QUEUE) -> Queue:
    settings = get_settings()
    return Queue(name, connection=Redis.from_url(settings.redis_url))


def enqueue_process_message(message_id: str, correlation_id: str = "",
                              delay_seconds: int = 0) -> None:
    if delay_seconds > 0:
        import datetime as _dt

        _queue().enqueue_in(
            _dt.timedelta(seconds=delay_seconds),
            "pia_worker.jobs.process_message.process_message",
            message_id,
            correlation_id,
            retry=Retry(max=3),
        )
        return
    _queue().enqueue(
        "pia_worker.jobs.process_message.process_message",
        message_id,
        correlation_id,
        retry=Retry(max=3),
    )


def enqueue_download_attachment(attachment_id: str, delay_seconds: int = 0) -> None:
    if delay_seconds > 0:
        import datetime as _dt

        _queue().enqueue_in(
            _dt.timedelta(seconds=delay_seconds),
            "pia_worker.jobs.process_message.download_attachment",
            attachment_id,
            retry=Retry(max=5),
        )
        return
    _queue().enqueue(
        "pia_worker.jobs.process_message.download_attachment",
        attachment_id,
        retry=Retry(max=5),
    )


def enqueue_form_submit(action_id: str) -> None:
    """P14.1: the owner approved a form_draft — hand it to the executor.
    No RQ retry: browser failures transition the action to FAILED in-job and
    are messaged; blind retries would double-drive a live form."""
    _queue().enqueue(
        "pia_worker.automation.executor.submit_form_action",
        action_id,
    )


def enqueue_proposal_executor(action_id: str) -> None:
    """ADR-013 Stage 3: the owner approved a reviewer proposal — hand it to
    the executor. Realtime: an approval the owner is waiting on must not sit
    behind a bulk flood. No RQ retry: failures transition the action to FAILED
    in-job (with Telegram fallback); blind retries would double-send notes."""
    _queue(REALTIME_QUEUE).enqueue(
        "pia_worker.executors.proposal_executors.run_proposal_executor",
        action_id,
    )
