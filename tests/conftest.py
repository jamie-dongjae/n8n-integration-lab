"""Pytest fixtures: isolated SQLite + a TestClient with known API keys.

Each test gets its own tempfile DB so there's no shared state.
"""
from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def api_env(monkeypatch, request):
    """Set env BEFORE importing the app module. Fault rate off by default; a
    test that wants faults sets `pytest.mark.parametrize('api_env', [...], indirect=True)`.
    """
    tmp = Path(tempfile.mkdtemp()) / "lab.db"
    monkeypatch.setenv("DB_PATH", str(tmp))
    monkeypatch.setenv("API_KEY_RW", "test-rw-key")
    monkeypatch.setenv("API_KEY_RO", "test-ro-key")
    # High rate so rate-limit tests opt in explicitly via the fault_env fixture.
    monkeypatch.setenv("RATE_LIMIT_RPS", "1000")
    monkeypatch.setenv("FAULT_RATE", "0.0")
    overrides = getattr(request, "param", {}) or {}
    for k, v in overrides.items():
        monkeypatch.setenv(k, str(v))
    return tmp


@pytest.fixture
def app_client(api_env):
    # Reload so module-level env reads see the new values.
    from mock_apis import db, main
    importlib.reload(db)
    importlib.reload(main)
    db.init()
    with TestClient(main.app) as client:
        yield client


@pytest.fixture
def rw_headers() -> dict:
    return {"X-API-Key": "test-rw-key"}


@pytest.fixture
def ro_headers() -> dict:
    return {"X-API-Key": "test-ro-key"}


# Base tenant payload fixture — one shape used by many tests.
@pytest.fixture
def tenant_payload() -> dict:
    return {
        "external_id": "H-0001",
        "name": "Example Tenant B.V.",
        "chamber_of_commerce_id": "12345678",
        "unit": "U-001",
        "lease_start": "2024-01-15",
        "monthly_rent_eur": 1250.00,
        "status": "active",
    }
