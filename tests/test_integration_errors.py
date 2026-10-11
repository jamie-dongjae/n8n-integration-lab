"""Integration-error endpoints — WF2 depends on these."""
from __future__ import annotations


def test_report_and_list_new(app_client, rw_headers):
    body = {"source": "WF1", "payload": {"huurder_id": "H-9001"}, "http_status": 422,
            "error_body": "{\"error\":\"validation\"}"}
    r = app_client.post("/platform/v1/integration-errors", json=body, headers=rw_headers)
    assert r.status_code == 201
    err_id = r.json()["id"]

    r = app_client.get("/platform/v1/integration-errors?status=new", headers=rw_headers)
    assert r.status_code == 200
    ids = [item["id"] for item in r.json()["items"]]
    assert err_id in ids


def test_patch_marks_triaged(app_client, rw_headers):
    created = app_client.post(
        "/platform/v1/integration-errors",
        json={"source": "WF1", "payload": {"foo": "bar"}, "http_status": 429},
        headers=rw_headers,
    ).json()
    patch = {
        "category": "rate_limit",
        "likely_cause": "Platform exceeded 5 rps.",
        "suggested_fix": "Lower batch size or add jitter between requests.",
        "client_message": "The sync is backing off because of a rate limit. It will retry automatically.",
        "confidence": 0.9,
        "model": "rules",
    }
    r = app_client.patch(f"/platform/v1/integration-errors/{created['id']}",
                         json=patch, headers=rw_headers)
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "triaged"
    assert out["triage"]["category"] == "rate_limit"


def test_patch_missing_404(app_client, rw_headers):
    patch = {
        "category": "unknown", "likely_cause": "x", "suggested_fix": "y",
        "client_message": "z", "confidence": 0.5,
    }
    r = app_client.patch("/platform/v1/integration-errors/99999",
                         json=patch, headers=rw_headers)
    assert r.status_code == 404
