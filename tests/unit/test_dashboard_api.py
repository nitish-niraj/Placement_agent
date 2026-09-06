"""Dashboard API auth contract (SEC-002 posture): every business endpoint sits
behind the bearer token; validation errors are user-facing, not crashes."""

import pytest
from fastapi.testclient import TestClient

from pia_api.main import create_app
from pia_api.settings import Settings

TOKEN = "test-dashboard-token"

ENDPOINTS = [
    ("get", "/api/v1/overview"), ("get", "/api/v1/eligibility"),
    ("get", "/api/v1/companies"), ("get", "/api/v1/events"),
    ("get", "/api/v1/deadlines"), ("get", "/api/v1/notifications"),
    ("get", "/api/v1/messages"), ("get", "/api/v1/groups"),
    ("get", "/api/v1/audit"), ("get", "/api/v1/metrics"),
    ("get", "/api/v1/documents"), ("get", "/api/v1/profile"),
    ("post", "/api/v1/ask"), ("patch", "/api/v1/groups/00000000-0000-0000-0000-000000000001"),
]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        "pia_api.settings._settings",
        Settings(_env_file=None, dashboard_token=TOKEN),  # type: ignore[call-arg]
    )
    return TestClient(create_app())


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_endpoints_require_token(client: TestClient, method: str, path: str) -> None:
    response = getattr(client, method)(path)
    assert response.status_code == 401, path


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_endpoints_reject_wrong_token(client: TestClient, method: str,
                                      path: str) -> None:
    response = getattr(client, method)(path, headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401, path


def test_ask_validates_question_length(client: TestClient) -> None:
    # Body validation happens before any DB/LLM access.
    response = client.post("/api/v1/ask", json={"question": "hi"},
                           headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 422


def test_group_patch_validates_category(client: TestClient) -> None:
    response = client.patch(
        "/api/v1/groups/00000000-0000-0000-0000-000000000001",
        json={"category": "NOT_A_CATEGORY"},
        headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 422

    response = client.patch(
        "/api/v1/groups/00000000-0000-0000-0000-000000000001",
        json={},
        headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 422  # no fields to update
