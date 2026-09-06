"""Integration smoke tests — run only against the live compose stack:

    docker compose -f infrastructure/docker-compose.yml up -d
    PIA_RUN_INTEGRATION=1 pytest -m integration
"""

import os

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = os.getenv("PIA_API_URL", "http://localhost:8000")


@pytest.mark.skipif(
    os.getenv("PIA_RUN_INTEGRATION") != "1",
    reason="Integration tests require the compose stack (PIA_RUN_INTEGRATION=1).",
)
def test_health_is_green() -> None:
    response = httpx.get(f"{BASE_URL}/health", timeout=10)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}  # worker heartbeat may lag at boot
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "ok"


@pytest.mark.skipif(
    os.getenv("PIA_RUN_INTEGRATION") != "1",
    reason="Integration tests require the compose stack (PIA_RUN_INTEGRATION=1).",
)
def test_protected_route_rejects_anonymous() -> None:
    response = httpx.get(f"{BASE_URL}/api/v1/ping", timeout=10)
    assert response.status_code == 401


@pytest.mark.skipif(
    os.getenv("PIA_RUN_INTEGRATION") != "1",
    reason="Integration tests require the compose stack (PIA_RUN_INTEGRATION=1).",
)
def test_ten_duplicate_deliveries_create_one_message() -> None:
    """F-008 acceptance / NFR-002: 10 identical webhook deliveries -> one logical message."""
    import os as _os
    import uuid as _uuid

    secret = _os.environ["EVOLUTION_WEBHOOK_SECRET"]
    headers = {"X-PIA-Token": secret}
    event_id = f"FAKEID-{_uuid.uuid4()}"
    payload = {
        "event": "messages.upsert",
        "instance": "pia",
        "data": {
            "key": {
                "remoteJid": "120363407905418470@g.us",  # Placement MCA 2027 (enabled)
                "fromMe": False,
                "id": event_id,
                "participant": "919999999999@s.whatsapp.net",
            },
            "pushName": "Integration Test",
            "messageType": "conversation",
            "message": {"conversation": "idempotency probe"},
            "messageTimestamp": "1700000000",
        },
    }
    statuses = [
        httpx.post(f"{BASE_URL}/webhooks/evolution", json=payload, headers=headers, timeout=10)
        for _ in range(10)
    ]
    assert all(r.status_code == 202 for r in statuses)
    bodies = [r.json() for r in statuses]
    assert sum(b["stored"] for b in bodies) == 1, "exactly one stored message"
    assert sum(b["duplicates"] for b in bodies) == 9, "nine collapsed duplicates"
