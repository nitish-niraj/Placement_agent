"""Webhook security contract (SEC-003): fail-closed secret + verified envelope."""

import pytest
from fastapi.testclient import TestClient

from pia_api.main import create_app
from pia_api.settings import Settings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        "pia_api.settings._settings",
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            evolution_webhook_secret="test-webhook-secret",
            dashboard_token="t",
        ),
    )
    return TestClient(create_app())


def test_rejects_missing_token(client: TestClient) -> None:
    response = client.post("/webhooks/evolution", json={})
    assert response.status_code == 403


def test_rejects_wrong_token(client: TestClient) -> None:
    response = client.post(
        "/webhooks/evolution", json={}, headers={"X-PIA-Token": "nope"}
    )
    assert response.status_code == 403


def test_fail_closed_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pia_api.settings._settings",
        Settings(_env_file=None, evolution_webhook_secret=""),  # type: ignore[call-arg]
    )
    client = TestClient(create_app())
    response = client.post(
        "/webhooks/evolution", json={}, headers={"X-PIA-Token": "anything"}
    )
    assert response.status_code == 403


def test_accepts_unknown_event_with_valid_token(client: TestClient) -> None:
    """Unknown/unhandled events are accepted (202) but counted as ignored — no DB hit."""
    response = client.post(
        "/webhooks/evolution",
        json={"event": "presence.update", "instance": "pia", "data": {}},
        headers={"X-PIA-Token": "test-webhook-secret"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["ignored"] == 1


def test_malformed_json_is_422(client: TestClient) -> None:
    response = client.post(
        "/webhooks/evolution",
        content=b"not-json",
        headers={"X-PIA-Token": "test-webhook-secret", "Content-Type": "application/json"},
    )
    assert response.status_code == 422
