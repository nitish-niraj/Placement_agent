"""Failed-job watchdog (operations hardening).

RQ's failed registry is memory-only institutional knowledge: entries vanish on
a Redis restart (exactly what happened to 214 dead jobs) with no trace of why
the pipeline bled. This hourly scan persists continuity itself in Redis
(survives restarts now that AOF is on) and routes genuinely NEW failures to
observability — structured logs always, full detail to the optional admin
channel. The student channel NEVER receives exception text, tracebacks, job
names, or queue internals: those are not placement information. The one
student-facing line is a plain-language stale-heartbeat note (delivery may
be affected), because that one is actually about their alerts."""

import json

import structlog
from rq import Queue
from rq.job import Job
from rq.registry import FailedJobRegistry

from pia_shared.queues import KNOWN_QUEUES
from pia_worker.jobs.demo import HEARTBEAT_KEY
from pia_worker.queue import make_connection
from pia_worker.settings import get_settings
from pia_worker.teams.listener import _telegram_send

logger = structlog.get_logger()

_STATE_KEY = "dlq:watch:last"
_MAX_TRACKED = 500
_TAIL_CHARS = 300

_STUDENT_HEARTBEAT_NOTE = (
    "⚠️ Placement alerts may be delayed — the background worker looks stale. "
    "Your data is safe; this usually resolves on its own.")


def _snapshot(client) -> list[dict]:
    """Current failed jobs across the served queues (fetch failures are
    skipped — evicted jobs must not break the scan)."""
    found: list[dict] = []
    for name in sorted(KNOWN_QUEUES):
        queue = Queue(name, connection=client)
        for job_id in FailedJobRegistry(queue=queue).get_job_ids():
            try:
                job = Job.fetch(job_id, connection=client)
            except Exception:  # noqa: BLE001 — evicted between list and fetch
                continue
            exc = (job.exc_info or "")[-_TAIL_CHARS:]
            found.append({"id": job_id, "queue": name,
                          "func": job.func_name or "?", "error": exc})
    return found


def _load_seen(client) -> set[str]:
    try:
        raw = client.get(_STATE_KEY)
    except Exception:  # noqa: BLE001 — redis blip means "alert everything once"
        return set()
    if not raw:
        return set()
    try:
        return set(json.loads(raw if isinstance(raw, str) else raw.decode()))
    except (ValueError, AttributeError):
        return set()


def _store_seen(client, ids: set[str]) -> None:
    try:
        client.set(_STATE_KEY, json.dumps(sorted(ids)[-_MAX_TRACKED:]))
    except Exception:  # noqa: BLE001 — best-effort bookkeeping
        logger.warning("watchdog_state_unstored")


def _summarize(new_failures: list[dict]) -> str:
    from collections import Counter

    counts = Counter(f["func"] for f in new_failures)
    lines = [f"• <code>{func}</code> ×{n}" for func, n in counts.most_common(6)]
    samples = "\n".join(
        f"— {f['func'].split('.')[-1]}: {(f['error'].splitlines() or ['?'])[-1][:140]}"
        for f in new_failures[:3])
    return (f"🚨 <b>{len(new_failures)} new failed job(s)</b>\n"
            + "\n".join(lines) + (f"\n{samples}" if samples else ""))


def _heartbeat_age_seconds(client) -> float | None:
    """Seconds since the worker heartbeat; None when missing/unreadable."""
    import datetime as dt

    try:
        raw = client.get(HEARTBEAT_KEY)
    except Exception:  # noqa: BLE001 — redis blip reads as stale downstream
        return None
    if not raw:
        return None
    try:
        stamped = dt.datetime.fromisoformat(
            raw.decode() if isinstance(raw, bytes) else raw)
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=dt.UTC)
        return (dt.datetime.now(dt.UTC) - stamped).total_seconds()
    except (ValueError, AttributeError, OverflowError):
        return None


def scan_failed_jobs() -> dict:
    """Hourly entry: DLQ delta + queue-depth + heartbeat freshness. Telegrams
    one combined digest when anything is noteworthy, otherwise stays quiet.
    Returns counts (job-friendly)."""
    client = make_connection()
    settings = get_settings()
    current = _snapshot(client)
    seen = _load_seen(client)
    current_ids = {f["id"] for f in current}
    new_failures = [f for f in current if f["id"] not in seen]
    _store_seen(client, current_ids)

    depths = {name: Queue(name, connection=client).count for name in KNOWN_QUEUES}
    max_depth = max(depths.values()) if depths else 0
    heartbeat_age = _heartbeat_age_seconds(client)
    heartbeat_stale = (heartbeat_age is None
                       or heartbeat_age > settings.watch_heartbeat_stale_seconds)

    sections = []
    if new_failures:
        sections.append(_summarize(new_failures))
        logger.warning("dlq_growth", new=len(new_failures), failed=len(current),
                       detail=" | ".join(
                           f"{f['func'].split('.')[-1]}: "
                           f"{(f['error'].splitlines() or ['?'])[-1][:140]}"
                           for f in new_failures[:5]))
    deep = {name: depth for name, depth in depths.items()
            if depth >= settings.watch_queue_depth_threshold}
    if deep:
        sections.append("📦 queue depth high: " + ", ".join(
            f"<code>{name}</code>={depth}" for name, depth in sorted(deep.items())))
    if heartbeat_stale:
        age = "missing" if heartbeat_age is None else f"{heartbeat_age:.0f}s old"
        sections.append(f"💔 worker heartbeat stale ({age}) — process may be down")

    # Routing: internals go to the admin channel (or logs only when it is
    # not configured). The student channel gets at most the plain-language
    # heartbeat note — never func names, traces, or queue internals.
    admin_chat = (settings.admin_telegram_chat_id or "").strip()
    debug = settings.debug_notifications_enabled
    try:
        if sections and (admin_chat or debug):
            _telegram_send(text="\n".join(sections),
                           chat_id=admin_chat or None)
        if heartbeat_stale:
            _telegram_send(text=_STUDENT_HEARTBEAT_NOTE)
    except Exception as exc:  # noqa: BLE001 — alerting must not fail scan
        logger.warning("watchdog_alert_failed", error=str(exc)[:120])
    if sections:
        return {"failed": len(current), "new": len(new_failures),
                "max_depth": max_depth, "heartbeat_stale": heartbeat_stale}
    return {"failed": len(current), "new": 0,
            "max_depth": max_depth, "heartbeat_stale": False}
