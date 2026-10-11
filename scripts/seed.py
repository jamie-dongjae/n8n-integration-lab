"""Deterministic seed: 50 ERP tenants with 6 broken records, 3 messy turnover CSVs.

Everything is derived from a fixed RNG seed so the sync numbers in the README
reproduce exactly. Synthetic names only; no real companies or people.
"""
from __future__ import annotations

import csv
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mock_apis import db  # noqa: E402

# 50 ERP tenants total: 44 clean + 5 intrinsically broken + 1 that looks valid
# in the ERP but collides with a pre-seeded platform row (different KvK) -> 409.
# Sync target: 44 upserted, 6 errors (5 x 422, 1 x 409).
SEED = 42
NUM_CLEAN = 44
NUM_INTRINSIC_BAD = 5  # bad_date, bad_kvk, unknown_status, missing_name, bad_amount
CENTRES = ["Westfield-ZH", "Stadshart-NB", "Centrumplein-UT"]
UNITS = [f"U-{n:03d}" for n in range(1, 60)]

# Synthetic Dutch-flavoured names (clearly fictional)
NAMES = [
    "De Groene Boon B.V.", "Fietspad Koffie", "Boek & Vink Antiek",
    "Tulp Technologie", "Blauwe Haring", "Oranje Optiek", "Zwart Op Wit Drukkerij",
    "Noord Zuid Lijn Cafe", "Wind in de Zeilen", "De Kleine Appelboom",
    "Rood Wit Blauw Streetwear", "Groot Formaat Repro", "Klompen & Kousen",
    "Nieuwe Grond Keramiek", "Zout op Dinsdag", "Hoog Bezoek Kappers",
    "Ruis Boekhandel", "Nachtschade Kruiden", "Blauwe Noot Jazzclub",
    "Zonder Namen Kunstkamer", "Vrij Wandelen Reisbureau", "Breed Water Zwemkleding",
    "Open Deur Timmerwerk", "Vliegenzwam Vegan", "Zonlicht Zonnebrillen",
    "Vier Jaargetijden Interieur", "Een Twee Drie Speelgoed", "Suiker Papier Bakkerij",
    "Hoge Pet Herenmode", "Vuur en Vlam Keukens", "Zilveren Lepel Lunchroom",
    "Groene Weide Zuivel", "Blauwe Vinvis Boekje", "Wit Zand Zonnepanelen",
    "Vier Zuilen Boekhouding", "Snelle Draai Fietsen", "Papieren Vlieger Kids",
    "Donker Hout Meubels", "Lentezon Bloemen", "Groen Dak Zaden",
    "Zacht Licht Lampen", "Twee Bergen Thee", "Wilgenbroek Vissport",
    "Hagelslag Hobbies", "Zomerzon Zonnewering", "Herfstvlieger Drones",
    "Winterlied Platenzaak", "Vier Winden Kompas", "Hoge Golven Surfshop",
    "Zuiderzee Zoutjes",
]


def _rand_kvk(rng: random.Random) -> str:
    # The platform demands exactly 8 digits. 10000000 - 99999999 covers that cleanly.
    return str(rng.randint(10_000_000, 99_999_999))


def _euros(rng: random.Random) -> str:
    """Format as the ERP emits it: '1.250,00' (NL locale, thousands dot, decimal comma)."""
    amount = rng.randint(50_000, 1_250_000) / 100  # 500.00 .. 12500.00
    whole, frac = divmod(round(amount * 100), 100)
    thousands = f"{whole:,}".replace(",", ".")
    return f"{thousands},{frac:02d}"


def _ddmmyyyy(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def _iso_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_erp(rng: random.Random):
    """Return (erp_rows, pre_seed_conflict_row).

    `erp_rows` holds all 50 ERP tenants (clean + intrinsically-bad + the conflict pivot).
    The last clean row is also the one whose huurder_id gets pre-seeded on the platform
    with a different KvK, so WF1 hits a 409 when it tries to upsert it.
    """
    base_dt = datetime(2026, 10, 1, 10, 0, 0)

    clean: list[dict] = []
    for i in range(NUM_CLEAN):
        start = date(2024, 1, 1) + timedelta(days=rng.randint(0, 650))
        gewijzigd = base_dt + timedelta(minutes=i * 3)
        clean.append({
            "huurder_id": f"H-{i+1:04d}",
            "naam": NAMES[i % len(NAMES)],
            "kvk_nummer": _rand_kvk(rng),
            "unit_code": rng.choice(UNITS),
            "contract_start": _ddmmyyyy(start),
            "maandhuur": _euros(rng),
            "status": rng.choices(["actief", "beeindigd"], weights=[0.85, 0.15])[0],
            "gewijzigd_op": _iso_dt(gewijzigd),
        })

    # Two clean rows share a KvK (one owner, two units) — legitimate, must not be an error.
    clean[7]["kvk_nummer"] = clean[3]["kvk_nummer"]
    clean[7]["unit_code"] = "U-055"

    # The 409 pivot: a 45th ERP row that LOOKS valid on its own, but its huurder_id
    # gets pre-seeded on the platform with a different KvK below.
    conflict_row = {
        "huurder_id": "H-0045",
        "naam": "Collision Rentals B.V.",
        "kvk_nummer": _rand_kvk(rng),
        "unit_code": "U-056",
        "contract_start": "01-01-2024",
        "maandhuur": "1.000,00",
        "status": "actief",
        "gewijzigd_op": _iso_dt(base_dt + timedelta(minutes=NUM_CLEAN * 3)),
    }
    ghost_kvk = _rand_kvk(rng)
    while ghost_kvk == conflict_row["kvk_nummer"]:
        ghost_kvk = _rand_kvk(rng)

    # 5 intrinsically broken rows — one bug per row so the error log reads like a checklist.
    intrinsic_bad: list[dict] = [
        {   # bad date
            "huurder_id": "H-9001", "naam": "Scherven Brengen Geluk",
            "kvk_nummer": _rand_kvk(rng), "unit_code": "U-021",
            "contract_start": "32-13-2024",
            "maandhuur": "900,00", "status": "actief",
            "gewijzigd_op": _iso_dt(base_dt + timedelta(hours=1)),
        },
        {   # bad KvK (7 digits)
            "huurder_id": "H-9002", "naam": "Zeven Cijfers Boekhouding",
            "kvk_nummer": "1234567", "unit_code": "U-022",
            "contract_start": "01-06-2025", "maandhuur": "1.100,00",
            "status": "actief", "gewijzigd_op": _iso_dt(base_dt + timedelta(hours=2)),
        },
        {   # unknown status
            "huurder_id": "H-9003", "naam": "Twijfel & Co",
            "kvk_nummer": _rand_kvk(rng), "unit_code": "U-023",
            "contract_start": "15-02-2025", "maandhuur": "750,00",
            "status": "misschien", "gewijzigd_op": _iso_dt(base_dt + timedelta(hours=3)),
        },
        {   # missing name
            "huurder_id": "H-9004", "naam": None,
            "kvk_nummer": _rand_kvk(rng), "unit_code": "U-024",
            "contract_start": "01-01-2024", "maandhuur": "1.500,00",
            "status": "actief", "gewijzigd_op": _iso_dt(base_dt + timedelta(hours=4)),
        },
        {   # malformed amount
            "huurder_id": "H-9005", "naam": "Komma Erbij",
            "kvk_nummer": _rand_kvk(rng), "unit_code": "U-025",
            "contract_start": "01-03-2025", "maandhuur": "duizend euro",
            "status": "actief", "gewijzigd_op": _iso_dt(base_dt + timedelta(hours=5)),
        },
    ]

    erp_rows = clean + intrinsic_bad + [conflict_row]
    assert len(erp_rows) == NUM_CLEAN + NUM_INTRINSIC_BAD + 1 == 50, "ERP seed must be 50 rows"

    pre_seed = {
        "external_id": conflict_row["huurder_id"],
        "name": conflict_row["naam"],
        "chamber_of_commerce_id": ghost_kvk,
        "unit": conflict_row["unit_code"],
        "lease_start": "2024-01-01",
        "monthly_rent_eur": 1000.0,
        "status": "active",
    }
    return erp_rows, pre_seed


def generate_turnover(rng: random.Random, external_ids: list[str]) -> dict[str, list[list[str]]]:
    """Three CSVs, each messy in a different way. Rows total ~2,000."""
    files: dict[str, list[list[str]]] = {}

    # File 1 — clean-ish, comma delimiter, YYYY-MM period.
    rows: list[list[str]] = [["external_id", "centre", "period", "amount_eur"]]
    for xid in external_ids[:30]:
        for m in range(12):
            period = f"2025-{m+1:02d}"
            amount = round(rng.uniform(500, 25000), 2)
            rows.append([xid, "Westfield-ZH", period, f"{amount:.2f}"])
    files["westfield_zh_2025.csv"] = rows

    # File 2 — semicolon, decimal commas, period "03/2025", a few duplicates, mixed case headers.
    rows2: list[list[str]] = [["EXTERNAL_ID", "Centre", "PERIOD", "amount_eur"]]
    for xid in external_ids[10:40]:
        for m in range(12):
            period = f"{m+1:02d}/2025"
            amount = round(rng.uniform(500, 20000), 2)
            rows2.append([xid, "Stadshart-NB", period, f"{amount:.2f}".replace(".", ",")])
    # Six exact dupes, two near-dupes with different capitalisation on the centre
    for _ in range(6):
        rows2.append(rows2[rng.randint(1, len(rows2) - 1)])
    files["stadshart_nb_2025.csv"] = rows2

    # File 3 — Dutch month words, odd columns in a different order, bad references, negative corrections.
    nl_months = ["januari", "februari", "maart", "april", "mei", "juni",
                 "juli", "augustus", "september", "oktober", "november", "december"]
    rows3: list[list[str]] = [["centre", "tenant_ref", "period", "amount_eur"]]
    for xid in external_ids[20:50]:
        for i, m in enumerate(nl_months):
            period = f"{m} 2025"
            amount = round(rng.uniform(600, 15000), 2)
            rows3.append(["Centrumplein-UT", xid, period, f"{amount:.2f}"])
    # Unknown tenant references (bad data)
    for ghost in ["H-9999", "H-8888", "H-7777"]:
        rows3.append(["Centrumplein-UT", ghost, "maart 2025", "123.45"])
    # Three negative correction rows
    for _ in range(3):
        xid = rng.choice(external_ids[20:50])
        rows3.append(["Centrumplein-UT", xid, "mei 2025", f"-{rng.randint(100, 2000):.2f}"])
    files["centrumplein_ut_2025.csv"] = rows3

    return files


def write_csv(path: Path, rows: list[list[str]], delimiter: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=delimiter)
        w.writerows(rows)


def main() -> int:
    db.reset()
    rng = random.Random(SEED)

    erp_rows, pre_seed = generate_erp(rng)
    with db.connect() as conn:
        for r in erp_rows:
            conn.execute(
                """INSERT INTO erp_tenants(huurder_id, naam, kvk_nummer, unit_code,
                                            contract_start, maandhuur, status, gewijzigd_op)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (r["huurder_id"], r["naam"], r["kvk_nummer"], r["unit_code"],
                 r["contract_start"], r["maandhuur"], r["status"], r["gewijzigd_op"]),
            )
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        conn.execute(
            """INSERT INTO platform_tenants(external_id, name, chamber_of_commerce_id, unit,
                                             lease_start, monthly_rent_eur, status, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pre_seed["external_id"], pre_seed["name"], pre_seed["chamber_of_commerce_id"],
             pre_seed["unit"], pre_seed["lease_start"], pre_seed["monthly_rent_eur"],
             pre_seed["status"], now),
        )

    # Clean tenants are H-0001..H-00NUM_CLEAN; use them as turnover references.
    clean_ids = [f"H-{i+1:04d}" for i in range(NUM_CLEAN)]
    files = generate_turnover(rng, clean_ids)
    out_dir = ROOT / "data" / "turnover"
    for name, rows in files.items():
        delim = ";" if name.startswith("stadshart") else ","
        write_csv(out_dir / name, rows, delim)

    print(f"seeded {len(erp_rows)} ERP tenants ({NUM_CLEAN} clean + {NUM_INTRINSIC_BAD} broken + 1 conflict pivot) into {db.db_path()}")
    print(f"pre-seeded 1 platform tenant with a conflicting KvK: {pre_seed['external_id']}")
    print(f"wrote {len(files)} turnover CSVs under {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
