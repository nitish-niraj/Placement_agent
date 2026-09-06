"""F-006 group discovery: pull the group list from Evolution API and upsert into `groups`.

Usage (from repo root, with env from infrastructure/.env):
    python -m pia_api.sync_groups

Every discovered group lands with enabled=false — the owner opts in explicitly
(FR-WA-004, SEC-006: only allowlisted groups are ever processed).
"""

import os
import sys
import urllib.request

import sqlalchemy

from pia_api.db import get_engine

SQL_UPSERT = sqlalchemy.text(
    "INSERT INTO groups (provider_group_id, name, participant_meta) "
    "VALUES (:gid, :name, CAST(:meta AS jsonb)) "
    "ON CONFLICT (provider_group_id) DO UPDATE "
    "SET name = EXCLUDED.name, participant_meta = EXCLUDED.participant_meta, updated_at = now()"
)


def _api_get(base_url: str, api_key: str, path: str) -> object:
    req = urllib.request.Request(base_url.rstrip("/") + path, headers={"apikey": api_key})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json_loads(response.read())


def json_loads(raw: bytes):
    import json

    return json.loads(raw)


def main() -> None:
    base_url = os.getenv("EVOLUTION_BASE_URL", "http://localhost:8080")
    api_key = os.getenv("EVOLUTION_API_KEY", "")
    instance = os.getenv("EVOLUTION_INSTANCE_NAME", "pia")
    if not api_key:
        sys.exit("EVOLUTION_API_KEY not set (source infrastructure/.env)")

    data = _api_get(base_url, api_key, f"/group/fetchAllGroups/{instance}?getParticipants=false")
    if isinstance(data, dict):
        rows: object = data.get("groups", [])
    elif isinstance(data, list):
        rows = data
    else:
        sys.exit(f"unexpected group payload shape: {type(data).__name__}")
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list):
        sys.exit(f"unexpected group payload shape: {type(rows).__name__}")

    engine = get_engine()
    with engine.begin() as conn:
        for group in rows:
            gid = group.get("id")
            if not gid:
                continue
            conn.execute(
                SQL_UPSERT,
                {"gid": gid, "name": group.get("subject") or gid, "meta": _meta(group)},
            )
    engine.dispose()
    print(f"Synced {len(rows)} groups from instance '{instance}' (all disabled by default)")


def _meta(group: dict) -> str:
    import json

    keep = {k: group.get(k) for k in ("size", "owner", "creation", "desc") if group.get(k)}
    return json.dumps(keep, default=str)


if __name__ == "__main__":
    main()
