"""Seed is deterministic and matches the convergence target (44 good + 6 bad)."""
from __future__ import annotations

import os
from pathlib import Path


def test_seed_is_deterministic(tmp_path, monkeypatch):
    db_path = tmp_path / "lab.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    import importlib
    from mock_apis import db
    importlib.reload(db)
    from scripts import seed as seed_mod
    importlib.reload(seed_mod)
    seed_mod.main()

    with db.connect() as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM erp_tenants").fetchone()
    # 44 good + 6 bad = 50
    assert rows["n"] == 50

    # The ERP always ends up with the same list of huurder_ids given the fixed seed.
    with db.connect() as conn:
        ids = [r["huurder_id"] for r in conn.execute("SELECT huurder_id FROM erp_tenants ORDER BY huurder_id").fetchall()]
    assert ids[0].startswith("H-")
    assert "H-9001" in ids and "H-9006" not in ids  # bad rows go up to 9005; 9006 is NOT used


def test_pre_seed_conflict_row_present(tmp_path, monkeypatch):
    db_path = tmp_path / "lab.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    import importlib
    from mock_apis import db
    importlib.reload(db)
    from scripts import seed as seed_mod
    importlib.reload(seed_mod)
    seed_mod.main()

    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM platform_tenants").fetchone()[0]
    assert count == 1, "seed must leave exactly one pre-seeded platform tenant for the 409 case"


def test_turnover_csvs_generated(tmp_path, monkeypatch):
    db_path = tmp_path / "lab.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    import importlib
    from mock_apis import db
    importlib.reload(db)
    from scripts import seed as seed_mod
    importlib.reload(seed_mod)
    seed_mod.main()

    root = Path(__file__).resolve().parent.parent
    csvs = sorted((root / "data" / "turnover").glob("*.csv"))
    assert len(csvs) == 3
    sizes = [p.stat().st_size for p in csvs]
    assert all(s > 1000 for s in sizes), f"turnover files look empty: {list(zip(csvs, sizes))}"
