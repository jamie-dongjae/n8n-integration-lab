"""Python port of the n8n Code-node rule-based triage.

The n8n workflow's `Rule-based triage (mock)` node and this module must stay in
lockstep. Keeping both means:
- The eval can run offline (no n8n, no Anthropic key).
- The CI job gets a deterministic triage accuracy number for the mock path.
- When the Anthropic Chat Model is wired up, the eval harness swaps in HTTP calls
  to the WF2 webhook without changing the case file.
"""
from __future__ import annotations

from typing import Any


Category = str  # auth | validation | mapping | rate_limit | upstream_outage | duplicate | unknown


def _code_in_body(body: str, code: str) -> bool:
    return code in body


def classify(record: dict[str, Any]) -> dict[str, Any]:
    body = str(record.get("error_body") or "")
    status = record.get("http_status")
    try:
        status = int(status) if status is not None else 0
    except (TypeError, ValueError):
        status = 0

    out = {
        "category": "unknown",
        "likely_cause": "Could not classify from the available fields.",
        "suggested_fix": "Open the error payload and compare against docs/mapping.md.",
        "client_message": "We hit a problem syncing this record; our team is looking at it.",
        "confidence": 0.4,
        "model": "rules",
    }

    if status in (401,) or _code_in_body(body, "invalid_api_key"):
        out.update(category="auth",
                   likely_cause="Platform rejected the X-API-Key.",
                   suggested_fix="Rotate the API key in n8n credentials and verify it has write scope.",
                   client_message="Access to the platform was denied. The team is refreshing credentials and will retry.",
                   confidence=0.85)
        return out
    if status in (403,) or _code_in_body(body, "read_only_key"):
        out.update(category="auth",
                   likely_cause="Read-only key used for a write endpoint.",
                   suggested_fix="Switch the HTTP node to the write-scoped credential.",
                   client_message="A read-only credential was used for a write operation; corrected and retrying.",
                   confidence=0.9)
        return out
    if status == 409 or _code_in_body(body, "external_id_conflict"):
        out.update(category="duplicate",
                   likely_cause="external_id exists with a different chamber_of_commerce_id.",
                   suggested_fix="Escalate: a human needs to confirm which KvK is correct.",
                   client_message="This tenant could not be updated because the Chamber of Commerce number on file does not match. We are checking which one is correct.",
                   confidence=0.9)
        return out
    # Rate limit before generic 4xx validation.
    if status == 429 or _code_in_body(body, "rate_limited"):
        out.update(category="rate_limit",
                   likely_cause="Platform rate limit exceeded.",
                   suggested_fix="Lower n8n batching or add jitter between requests.",
                   client_message="The system is sending requests slightly faster than the platform accepts; it will slow down automatically.",
                   confidence=0.9)
        return out
    if status in (500, 501, 502, 503, 504) or _code_in_body(body, "upstream_outage"):
        out.update(category="upstream_outage",
                   likely_cause="Platform returned a 5xx after retries.",
                   suggested_fix="Check platform health. Keep the row in the retry queue.",
                   client_message="The platform was temporarily unavailable; the sync will retry.",
                   confidence=0.75)
        return out
    if status == 422 or ('"error":"validation"' in body):
        # Within validation, we flag "unknown_status" as a mapping bug — our vocabulary is wrong, not the data.
        if "unknown_status" in body:
            out.update(category="mapping",
                       likely_cause="Status value is not in the mapping dictionary.",
                       suggested_fix="Add the new status to the mapping in WF1 (docs/mapping.md).",
                       client_message="A tenant status value from the source system is not in our translation list yet. We will add it and re-sync.",
                       confidence=0.8)
            return out
        if "invalid_kvk" in body:
            cause = "KvK number is not 8 digits."
            msg = "Chamber of Commerce number has the wrong length."
        elif "invalid_date" in body:
            cause = "contract_start is not a real DD-MM-YYYY date."
            msg = "Lease start date is not a real date."
        elif "invalid_amount" in body:
            cause = "maandhuur is not a parseable number."
            msg = "Monthly rent value could not be read as a number."
        elif "missing_name" in body:
            cause = "Tenant name is empty."
            msg = "Tenant name was missing on the source record."
        else:
            cause = "Row failed canonical validation. See docs/mapping.md."
            msg = "Our platform rejected this tenant because a field was out of range."
        out.update(category="validation",
                   likely_cause=cause,
                   suggested_fix="Fix the source row in the ERP or add a mapping rule in WF1. Then re-run the sync.",
                   client_message=msg,
                   confidence=0.8)
        return out
    return out
