"""Tests for the health-check endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    """``GET /health`` responds 200 with the expected body."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_returns_service_metadata(client: TestClient) -> None:
    """``GET /`` exposes name, version and the health path."""
    response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["name"]
    assert body["version"]
    assert body["health"] == "/health"
