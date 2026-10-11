"""Turnover bulk endpoint: idempotency, batch cap, unknown references, dupes."""
from __future__ import annotations


def _ensure_tenant(client, headers, external_id="H-TURN-1", kvk="88888888"):
    body = {
        "external_id": external_id,
        "name": "Turnover Shop B.V.",
        "chamber_of_commerce_id": kvk,
        "unit": "U-010",
        "lease_start": "2024-01-01",
        "monthly_rent_eur": 1000.0,
        "status": "active",
    }
    r = client.post("/platform/v1/tenants", json=body, headers=headers)
    assert r.status_code == 200


def test_bulk_requires_idempotency_key(app_client, rw_headers):
    r = app_client.post("/platform/v1/turnover:bulk",
                        json={"rows": []}, headers=rw_headers)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "missing_idempotency_key"


def test_bulk_batch_cap_422(app_client, rw_headers):
    rows = [{"external_id": "x", "centre": "c", "period": "2025-01", "amount_eur": 1}] * 501
    r = app_client.post("/platform/v1/turnover:bulk", json={"rows": rows},
                        headers={**rw_headers, "Idempotency-Key": "k"})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "batch_too_large"


def test_bulk_unknown_tenant_reports_per_row_error(app_client, rw_headers):
    rows = [{"external_id": "H-GHOST", "centre": "x", "period": "2025-01", "amount_eur": 100}]
    r = app_client.post("/platform/v1/turnover:bulk", json={"rows": rows},
                        headers={**rw_headers, "Idempotency-Key": "k1"})
    assert r.status_code == 200
    body = r.json()
    assert body["errors"] == 1
    assert body["results"][0]["status"] == "error"
    assert "unknown" in body["results"][0]["error"].lower()


def test_bulk_duplicates_within_run(app_client, rw_headers):
    _ensure_tenant(app_client, rw_headers)
    row = {"external_id": "H-TURN-1", "centre": "C1", "period": "2025-03", "amount_eur": 500.0}
    r = app_client.post("/platform/v1/turnover:bulk", json={"rows": [row, row]},
                        headers={**rw_headers, "Idempotency-Key": "k-dup"})
    body = r.json()
    assert body["created"] == 1 and body["duplicates"] == 1


def test_bulk_idempotency_key_returns_cached(app_client, rw_headers):
    _ensure_tenant(app_client, rw_headers)
    row = {"external_id": "H-TURN-1", "centre": "C1", "period": "2025-04", "amount_eur": 700.0}
    k = "stable-key-42"
    first = app_client.post("/platform/v1/turnover:bulk", json={"rows": [row]},
                            headers={**rw_headers, "Idempotency-Key": k}).json()
    second = app_client.post("/platform/v1/turnover:bulk", json={"rows": [row]},
                             headers={**rw_headers, "Idempotency-Key": k}).json()
    assert first == second
    assert first["created"] == 1
    # No second insert happened:
    third = app_client.post("/platform/v1/turnover:bulk", json={"rows": [row]},
                            headers={**rw_headers, "Idempotency-Key": "fresh-key"}).json()
    assert third["duplicates"] == 1 and third["created"] == 0
