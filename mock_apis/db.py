"""SQLite layer for the mock APIs.

One file, one connection per request. Schema lives here so the seed script and
the tests can import the same DDL.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "lab.db"


def db_path() -> Path:
    return Path(os.environ.get("DB_PATH") or DEFAULT_DB_PATH)


SCHEMA = """
CREATE TABLE IF NOT EXISTS erp_tenants (
    huurder_id       TEXT PRIMARY KEY,
    naam             TEXT,
    kvk_nummer       TEXT,
    unit_code        TEXT,
    contract_start   TEXT,
    maandhuur        TEXT,
    status           TEXT,
    gewijzigd_op     TEXT
);

CREATE TABLE IF NOT EXISTS platform_tenants (
    external_id              TEXT PRIMARY KEY,
    name                     TEXT NOT NULL,
    chamber_of_commerce_id   TEXT NOT NULL,
    unit                     TEXT NOT NULL,
    lease_start              TEXT NOT NULL,
    monthly_rent_eur         REAL NOT NULL,
    status                   TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turnover (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id   TEXT NOT NULL,
    centre        TEXT NOT NULL,
    period        TEXT NOT NULL,
    amount_eur    REAL NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE(external_id, centre, period)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key           TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS integration_errors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    http_status   INTEGER,
    error_body    TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    triage_json   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_errors_status  ON integration_errors(status);
CREATE INDEX IF NOT EXISTS ix_erp_updated_on ON erp_tenants(gewijzigd_op);
"""


def init(path: Path | None = None) -> None:
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect(path: Path | None = None):
    """A short-lived connection with Row factory + foreign keys on."""
    p = Path(path) if path else db_path()
    conn = sqlite3.connect(p, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def reset(path: Path | None = None) -> None:
    p = Path(path) if path else db_path()
    if p.exists():
        p.unlink()
    init(p)
