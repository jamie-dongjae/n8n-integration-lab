"""FastAPI mock serving a legacy ERP and a canonical Tenant Platform.

Realistic failure modes a Solutions Engineer meets in the wild:
- 401 missing / unknown API key
- 403 read-only key hitting a write endpoint
- 409 external_id collision with a different KvK number
- 422 Pydantic validation body
- 429 Retry-After when the request rate goes above RATE_LIMIT_RPS
- 503 random fault when FAULT_RATE > 0 (for n8n retry demos)
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Deque

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import db
from .models import (
    IntegrationErrorIn,
    IntegrationErrorOut,
    PlatformTenantIn,
    PlatformTenantOut,
    TriagePatch,
    TurnoverBulkIn,
    TurnoverBulkOut,
    TurnoverRow,
    TurnoverRowResult,
)

API_KEY_RW = os.environ.get("API_KEY_RW", "rw-dev-local")
API_KEY_RO = os.environ.get("API_KEY_RO", "ro-dev-local")
RATE_LIMIT_RPS = int(os.environ.get("RATE_LIMIT_RPS", "5"))
FAULT_RATE = float(os.environ.get("FAULT_RATE", "0.0"))

# A tiny sliding-window rate limiter is enough for the demo. Keep it global so
# n8n's retries actually hit the 429 branch under load.
_RL_WINDOW: Deque[float] = deque()
_RL_LOCK_SECONDS = 1.0

# Deterministic fault injection: a counter per path makes CI runs reproducible
# when FAULT_RATE is set, instead of flaky "sometimes 503".
_fault_rng = random.Random(0xC0FFEE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# -------- auth / limits --------

def require_key(x_api_key: str | None = Header(default=None)) -> str:
    if not x_api_key or x_api_key not in {API_KEY_RW, API_KEY_RO}:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})
    return x_api_key


def require_rw_key(key: str = Depends(require_key)) -> str:
    if key != API_KEY_RW:
        raise HTTPException(status_code=403, detail={"error": "read_only_key"})
    return key


def _rate_limit(request: Request) -> None:
    # /health is unmetered so the readiness probe never 429s.
    if request.url.path == "/health":
        return
    now = time.monotonic()
    while _RL_WINDOW and now - _RL_WINDOW[0] > _RL_LOCK_SECONDS:
        _RL_WINDOW.popleft()
    if len(_RL_WINDOW) >= RATE_LIMIT_RPS:
        retry = max(0.05, _RL_LOCK_SECONDS - (now - _RL_WINDOW[0]))
        raise HTTPException(
            status_code=429,
            detail={"error": "rate_limited", "limit_rps": RATE_LIMIT_RPS},
            headers={"Retry-After": f"{retry:.2f}"},
        )
    _RL_WINDOW.append(now)


def _maybe_fault(request: Request) -> None:
    if request.url.path == "/health" or FAULT_RATE <= 0:
        return
    # Decide per request. A fixed-seed RNG keeps CI runs reproducible when
    # FAULT_RATE is on; swap for secrets.SystemRandom if real jitter is wanted.
    if _fault_rng.random() < FAULT_RATE:
        raise HTTPException(
            status_code=503,
            detail={"error": "upstream_outage"},
            headers={"Retry-After": "1"},
        )


# -------- app --------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    yield


app = FastAPI(title="n8n-integration-lab mock APIs", version="1.0.0", lifespan=lifespan)


@app.exception_handler(ValidationError)
async def pydantic_handler(_req, exc: ValidationError):
    return JSONResponse(status_code=422, content={"error": "validation", "fields": exc.errors()})


# -------- /health --------

@app.get("/health")
def health() -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM erp_tenants").fetchone()
    return {
        "ok": True,
        "erp_tenants": row["n"],
        "rate_limit_rps": RATE_LIMIT_RPS,
        "fault_rate": FAULT_RATE,
    }


# -------- /erp/v1 --------

@app.get("/erp/v1/huurders")
def list_huurders(
    request: Request,
    updated_since: str | None = Query(default=None),
    _rl: None = Depends(_rate_limit),
    _f: None = Depends(_maybe_fault),
    _k: str = Depends(require_key),
) -> dict:
    q = "SELECT * FROM erp_tenants"
    args: tuple = ()
    if updated_since:
        q += " WHERE gewijzigd_op >= ?"
        args = (updated_since,)
    q += " ORDER BY gewijzigd_op ASC"
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    return {"updated_since": updated_since, "count": len(rows), "huurders": rows}


# -------- /platform/v1/tenants --------

@app.post("/platform/v1/tenants", status_code=200)
def upsert_tenant(
    body: PlatformTenantIn,
    request: Request,
    _rl: None = Depends(_rate_limit),
    _f: None = Depends(_maybe_fault),
    _k: str = Depends(require_rw_key),
) -> PlatformTenantOut:
    now = _now_iso()
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT * FROM platform_tenants WHERE external_id = ?", (body.external_id,)
        ).fetchone()
        if existing and existing["chamber_of_commerce_id"] != body.chamber_of_commerce_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "external_id_conflict",
                    "message": "external_id exists with a different chamber_of_commerce_id",
                    "existing_chamber_of_commerce_id": existing["chamber_of_commerce_id"],
                    "incoming_chamber_of_commerce_id": body.chamber_of_commerce_id,
                },
            )
        conn.execute(
            """
            INSERT INTO platform_tenants(external_id, name, chamber_of_commerce_id, unit,
                                          lease_start, monthly_rent_eur, status, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(external_id) DO UPDATE SET
                name = excluded.name,
                chamber_of_commerce_id = excluded.chamber_of_commerce_id,
                unit = excluded.unit,
                lease_start = excluded.lease_start,
                monthly_rent_eur = excluded.monthly_rent_eur,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (body.external_id, body.name, body.chamber_of_commerce_id, body.unit,
             body.lease_start, body.monthly_rent_eur, body.status, now),
        )
    return PlatformTenantOut(**body.model_dump(), updated_at=now)


@app.get("/platform/v1/tenants/{external_id}")
def get_tenant(
    external_id: str,
    request: Request,
    _rl: None = Depends(_rate_limit),
    _f: None = Depends(_maybe_fault),
    _k: str = Depends(require_key),
) -> PlatformTenantOut:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM platform_tenants WHERE external_id = ?", (external_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    return PlatformTenantOut(**dict(row))


# -------- /platform/v1/turnover:bulk --------

MAX_TURNOVER_BATCH = 500


@app.post("/platform/v1/turnover:bulk")
def turnover_bulk(
    body: TurnoverBulkIn,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    _rl: None = Depends(_rate_limit),
    _f: None = Depends(_maybe_fault),
    _k: str = Depends(require_rw_key),
) -> TurnoverBulkOut:
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail={"error": "missing_idempotency_key",
                    "message": "Idempotency-Key header is required on turnover:bulk"},
        )
    if len(body.rows) > MAX_TURNOVER_BATCH:
        raise HTTPException(
            status_code=422,
            detail={"error": "batch_too_large", "max": MAX_TURNOVER_BATCH, "got": len(body.rows)},
        )
    with db.connect() as conn:
        cached = conn.execute(
            "SELECT response_json FROM idempotency_keys WHERE key = ?", (idempotency_key,)
        ).fetchone()
        if cached:
            return TurnoverBulkOut.model_validate_json(cached["response_json"])

        known_tenants = {
            r["external_id"] for r in conn.execute(
                "SELECT external_id FROM platform_tenants"
            ).fetchall()
        }
        results: list[TurnoverRowResult] = []
        created = duplicates = errors = 0
        for i, row in enumerate(body.rows):
            if row.external_id not in known_tenants:
                results.append(TurnoverRowResult(index=i, status="error",
                                                 error="unknown external_id"))
                errors += 1
                continue
            try:
                conn.execute(
                    "INSERT INTO turnover(external_id, centre, period, amount_eur, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (row.external_id, row.centre, row.period, row.amount_eur, _now_iso()),
                )
                created += 1
                results.append(TurnoverRowResult(index=i, status="created"))
            except Exception:  # IntegrityError on UNIQUE(external_id, centre, period)
                duplicates += 1
                results.append(TurnoverRowResult(index=i, status="duplicate"))
        out = TurnoverBulkOut(idempotency_key=idempotency_key, received=len(body.rows),
                              created=created, duplicates=duplicates, errors=errors,
                              results=results)
        conn.execute(
            "INSERT INTO idempotency_keys(key, response_json, created_at) VALUES (?,?,?)",
            (idempotency_key, out.model_dump_json(), _now_iso()),
        )
    return out


# -------- /platform/v1/integration-errors --------

def _row_to_error(row: Any) -> IntegrationErrorOut:
    triage = None
    if row["triage_json"]:
        triage = TriagePatch.model_validate_json(row["triage_json"])
    return IntegrationErrorOut(
        id=row["id"],
        source=row["source"],
        payload=json.loads(row["payload_json"]),
        http_status=row["http_status"],
        error_body=row["error_body"],
        status=row["status"],
        triage=triage,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@app.post("/platform/v1/integration-errors", status_code=201)
def report_error(
    body: IntegrationErrorIn,
    request: Request,
    _rl: None = Depends(_rate_limit),
    _f: None = Depends(_maybe_fault),
    _k: str = Depends(require_rw_key),
) -> IntegrationErrorOut:
    now = _now_iso()
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO integration_errors(source, payload_json, http_status, error_body,
                                               status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (body.source, json.dumps(body.payload), body.http_status, body.error_body,
             "new", now, now),
        )
        row = conn.execute(
            "SELECT * FROM integration_errors WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _row_to_error(row)


@app.get("/platform/v1/integration-errors")
def list_errors(
    request: Request,
    status: str = Query(default="new"),
    _rl: None = Depends(_rate_limit),
    _f: None = Depends(_maybe_fault),
    _k: str = Depends(require_key),
) -> dict:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM integration_errors WHERE status = ? ORDER BY id ASC", (status,)
        ).fetchall()
    return {"status": status, "count": len(rows), "items": [_row_to_error(r).model_dump() for r in rows]}


@app.patch("/platform/v1/integration-errors/{error_id}")
def triage_error(
    error_id: int,
    body: TriagePatch,
    request: Request,
    _rl: None = Depends(_rate_limit),
    _f: None = Depends(_maybe_fault),
    _k: str = Depends(require_rw_key),
) -> IntegrationErrorOut:
    now = _now_iso()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM integration_errors WHERE id = ?", (error_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        conn.execute(
            """UPDATE integration_errors
               SET status = 'triaged', triage_json = ?, updated_at = ?
               WHERE id = ?""",
            (body.model_dump_json(), now, error_id),
        )
        row = conn.execute(
            "SELECT * FROM integration_errors WHERE id = ?", (error_id,)
        ).fetchone()
    return _row_to_error(row)


def stable_idempotency_key(centre: str, external_id: str, period: str, amount: float) -> str:
    """Exposed so the migration script and the server share one hash shape."""
    raw = f"{centre}|{external_id}|{period}|{amount:.2f}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
