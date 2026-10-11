"""Period parser, amount parser, dedupe, and full-run reconciliation (no HTTP)."""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import migrate_turnover as m


# -------- unit --------

@pytest.mark.parametrize("raw,expected", [
    ("2025-03", "2025-03"),
    ("03/2025", "2025-03"),
    ("3/2025", "2025-03"),
    ("maart 2025", "2025-03"),
    ("december 2026", "2026-12"),
    ("januari 2024", "2024-01"),
    ("", None),
    (None, None),
    ("foo", None),
    ("13-2025", None),       # No bare date here; period parser is month+year only
    ("2025/03", None),       # Not accepted on purpose; forces upstream to clean up
])
def test_parse_period(raw, expected):
    assert m.parse_period(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("1250.00", 1250.00),
    ("1250,00", 1250.00),
    ("1.250,00", 1250.00),  # NL thousand + decimal
    ("-500.00", -500.00),
    ("-500,00", -500.00),
    ("", None),
    ("abc", None),
    ("12three", None),
])
def test_parse_amount(raw, expected):
    assert m.parse_amount(raw) == expected


def test_idempotency_key_stable():
    k1 = m.idempotency_key("WZ", "H-1", "2025-03", 1000.00)
    k2 = m.idempotency_key("WZ", "H-1", "2025-03", 1000.00)
    k3 = m.idempotency_key("WZ", "H-1", "2025-03", 1000.01)
    assert k1 == k2 and k1 != k3
    assert len(k1) == 64


def test_dedupe_catches_exact_repeats():
    rows = [m.Row("C", "X", "2025-01", 10.0), m.Row("C", "X", "2025-01", 10.0),
            m.Row("C", "X", "2025-02", 10.0)]
    unique, dupes = m.dedupe(rows)
    assert len(unique) == 2 and len(dupes) == 1


# -------- full-run reconciliation --------

def test_end_to_end_dry_run_reconciles(tmp_path, monkeypatch):
    # Build a tiny messy CSV deck inline — we don't need the full seed here.
    csvs = tmp_path / "turnover"
    csvs.mkdir()
    (csvs / "a.csv").write_text(
        "external_id,centre,period,amount_eur\n"
        "H-1,C1,2025-01,1000.00\n"
        "H-1,C1,2025-01,1000.00\n"            # local dupe
        "H-2,C1,maart 2025,500,00\n"          # NL amount + Dutch month
        "H-3,C1,2025-01,abc\n"                # bad amount, local reject
        ",C1,2025-01,100.00\n"                # missing external_id, local reject
        "H-4,,2025-01,100.00\n"               # missing centre, local reject
        "H-5,C1,,100.00\n"                    # missing period, local reject
        "H-6,C1,2025-01,-50.00\n"             # negative correction, kept
        , encoding="utf-8",
    )
    reports = tmp_path / "reports"
    monkeypatch.setenv("MOCK_APIS_BASE_URL", "http://unused")  # not called in --dry-run
    rc = m.main([
        "--data-dir", str(csvs),
        "--batch-size", "100",
        "--dry-run",
        "--out-dir", str(reports),
    ])
    assert rc == 0
    md = (reports / "migration_report.md").read_text()
    # 8 data lines in the file.
    assert "read: **8**" in md
    assert "**OK**" in md


def test_rejected_rows_csv_written(tmp_path, monkeypatch):
    csvs = tmp_path / "turnover"
    csvs.mkdir()
    (csvs / "a.csv").write_text(
        "external_id,centre,period,amount_eur\n"
        ",C1,2025-01,100.00\n"
        "H-1,C1,,100.00\n"
        , encoding="utf-8",
    )
    reports = tmp_path / "reports"
    rc = m.main([
        "--data-dir", str(csvs),
        "--dry-run",
        "--out-dir", str(reports),
    ])
    assert rc == 0
    rejects = (reports / "rejected_rows.csv").read_text()
    assert "missing_external_id" in rejects
    assert "invalid_period" in rejects
