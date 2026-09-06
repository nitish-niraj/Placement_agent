"""Prometheus-style counters, Redis-backed (TRD §10.2 — minimal MVP form).

The API and the worker both hold a Redis connection; counters are plain INCR
keys so no extra infrastructure is needed. The dashboard can read them later;
today they surface via logs and the Redis CLI.
"""


def counter_key(name: str, **labels: str) -> str:
    label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return f"pia:metrics:{name}{{{label_str}}}"


def increment(client, name: str, **labels: str) -> int:
    """INCR a labelled counter; failures are swallowed (observability must
    never break the pipeline) and return -1 so callers can log them."""
    try:
        return int(client.incr(counter_key(name, **labels)))
    except Exception:  # noqa: BLE001 — metrics are best-effort
        return -1
