"""Python port of WF1's Code node — syncs ERP into the platform without n8n.

Only purpose: let someone clone the repo, run `make seed migrate`, and get a
clean reconciliation report without having to boot n8n first. The actual demo
of n8n running the same mapping remains the point of WF1; this is just a
dev-ergonomics helper.

Keep this in lock-step with the JS in `workflows/01-...json` under
"Map & validate". The docs/mapping.md file is the canonical spec for both.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATUS = {"actief": "active", "beeindigd": "ended", "beëindigd": "ended"}
_DD_MM_YYYY = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")


def strip_digits(s: str) -> str:
    return re.sub(r"\D+", "", s or "")


def parse_date(s: str) -> str | None:
    if not s:
        return None
    m = _DD_MM_YYYY.match(s)
    if not m:
        return None
    dd, mm, yyyy = m.groups()
    try:
        dt = datetime(int(yyyy), int(mm), int(dd))
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d")


def parse_amount(s: str) -> float | None:
    if s is None:
        return None
    t = (s or "").strip()
    t = re.sub(r"[^0-9,.\-]", "", t)
    if not t:
        return None
    try:
        return float(t.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def map_row(r: dict) -> tuple[dict | None, list[str]]:
    errors: list[str] = []
    name = (r.get("naam") or "").strip()
    if not name:
        errors.append("missing_name")
    kvk = strip_digits(r.get("kvk_nummer") or "")
    if not re.fullmatch(r"\d{8}", kvk):
        errors.append("invalid_kvk")
    lease_start = parse_date(r.get("contract_start") or "")
    if not lease_start:
        errors.append("invalid_date")
    rent = parse_amount(r.get("maandhuur"))
    if rent is None or rent < 0:
        errors.append("invalid_amount")
    canonical_status = STATUS.get((r.get("status") or "").lower())
    if not canonical_status:
        errors.append("unknown_status")
    if errors:
        return None, errors
    return {
        "external_id": r["huurder_id"], "name": name,
        "chamber_of_commerce_id": kvk, "unit": r["unit_code"],
        "lease_start": lease_start, "monthly_rent_eur": rent,
        "status": canonical_status,
    }, []


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default=os.environ.get("MOCK_APIS_BASE_URL", "http://localhost:8000"))
    p.add_argument("--api-key", default=os.environ.get("API_KEY_RW", "rw-dev-local-change-me"))
    args = p.parse_args(argv)

    import random as _rand
    import time as _time

    def post_with_retry(c: httpx.Client, url: str, json_body: dict, max_tries: int = 6):
        """429 + 5xx retry with Retry-After. Idempotency on this path is covered by
        upsert-by-external_id; we don't need an Idempotency-Key header here."""
        for attempt in range(max_tries):
            r = c.post(url, json=json_body, headers=headers, timeout=15.0)
            if r.status_code == 429 and attempt < max_tries - 1:
                wait = float(r.headers.get("Retry-After", "1")) + _rand.uniform(0, 0.2)
                _time.sleep(wait)
                continue
            if 500 <= r.status_code < 600 and attempt < max_tries - 1:
                _time.sleep((2 ** attempt) * 0.3)
                continue
            return r
        return r  # last attempt result

    headers = {"X-API-Key": args.api_key}
    with httpx.Client() as c:
        erp = c.get(f"{args.base_url}/erp/v1/huurders", headers=headers, timeout=15.0)
        erp.raise_for_status()
        rows = erp.json()["huurders"]
        upserted, validation_errors, http_errors = 0, 0, 0
        err_summary: dict[str, int] = {}
        for r in rows:
            canonical, errors = map_row(r)
            if errors:
                validation_errors += 1
                for e in errors:
                    err_summary[e] = err_summary.get(e, 0) + 1
                post_with_retry(c, f"{args.base_url}/platform/v1/integration-errors",
                                {"source": "bootstrap_platform", "payload": r,
                                 "http_status": 422, "error_body": json.dumps({"codes": errors})})
                continue
            resp = post_with_retry(c, f"{args.base_url}/platform/v1/tenants", canonical)
            if resp.status_code == 200:
                upserted += 1
            else:
                http_errors += 1
                err_summary[f"http_{resp.status_code}"] = err_summary.get(f"http_{resp.status_code}", 0) + 1
                post_with_retry(c, f"{args.base_url}/platform/v1/integration-errors",
                                {"source": "bootstrap_platform", "payload": r,
                                 "http_status": resp.status_code, "error_body": resp.text})

    Path(ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "sync.md").write_text(
        f"# Sync (bootstrap_platform.py)\n\n"
        f"Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z\n\n"
        f"- rows seen: **{len(rows)}**\n"
        f"- upserted: **{upserted}**\n"
        f"- validation errors (422): **{validation_errors}**\n"
        f"- HTTP errors (409/5xx/...): **{http_errors}**\n\n"
        f"## Errors by reason\n" +
        ("\n".join(f"- `{k}`: {v}" for k, v in sorted(err_summary.items())) if err_summary else "_none_") +
        "\n", encoding="utf-8",
    )
    print(json.dumps({"rows": len(rows), "upserted": upserted,
                      "validation_errors": validation_errors, "http_errors": http_errors,
                      "reasons": err_summary}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
