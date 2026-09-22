"""Queue names shared by API (enqueue side) and worker (consume side).

- ``default`` — bulk pipeline: message parsing, attachments, extraction.
  Floods land here and drain whenever.
- ``realtime`` — user-facing: Telegram sends, approved-proposal executors.
  Never stuck behind a bulk flood.
- ``maintenance`` — scheduled sweeps/digests, watched by every worker.

Names live here (not in either app) so both sides agree without the API
importing worker code.
"""

DEFAULT_QUEUE = "default"
REALTIME_QUEUE = "realtime"
MAINTENANCE_QUEUE = "maintenance"

KNOWN_QUEUES = frozenset({DEFAULT_QUEUE, REALTIME_QUEUE, MAINTENANCE_QUEUE})


def parse_queue_names(spec: str | None) -> list[str]:
    """WORKER_QUEUES env → ordered queue list. Unknown names fail fast at
    boot (a typo must not silently create a queue nobody watches)."""
    names = [name.strip() for name in (spec or "").split(",") if name.strip()]
    resolved = names or [DEFAULT_QUEUE, MAINTENANCE_QUEUE]
    unknown = [name for name in resolved if name not in KNOWN_QUEUES]
    if unknown:
        raise ValueError(f"unknown queues in WORKER_QUEUES: {unknown}")
    return resolved
