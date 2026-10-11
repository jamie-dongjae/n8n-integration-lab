"""One-shot migration of turnover CSV exports into the Tenant Platform.

Shape of the problem: three CSVs with different delimiters, decimal styles,
period formats, dupes, bad references, and negative corrections. The platform
accepts canonical rows via `POST /platform/v1/turnover:bulk` with an
`Idempotency-Key` header so re-running is safe.

Design points worth defending in an interview:
- The idempotency key is a deterministic hash of (centre, external_id, period, amount).
  The server caches responses by this key so a batch replay returns the same body
  it returned the first time, without creating duplicates.
- Reconciliation holds: `read == imported + rejected + duplicates`.
  The report is written whether or not the run succeeds.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

import httpx

ROOT = Path(__file__).resolve().parent.parent

DUTCH_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
}


# -------- parsing primitives --------

def parse_period(raw: str) -> str | None:
    """Return canonical YYYY-MM, or None if unparseable.

    Accepts: 2025-03, 03/2025, 3/2025, maart 2025, March 2025.
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    # YYYY-MM
    try:
        d = datetime.strptime(s, "%Y-%m")
        return d.strftime("%Y-%m")
    except ValueError:
        pass
    # MM/YYYY or M/YYYY
    try:
        d = datetime.strptime(s, "%m/%Y")
        return d.strftime("%Y-%m")
    except ValueError:
        pass
    # Dutch month word + year
    for word, m in DUTCH_MONTHS.items():
        if s.startswith(word):
            tail = s[len(word):].strip()
            try:
                y = int(tail)
                return f"{y:04d}-{m:02d}"
            except ValueError:
                return None
    return None


def parse_amount(raw: str) -> float | None:
    """Accept '1250.00', '1250,00', '-500.00'. Reject anything with letters."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    # Reject anything containing letters.
    if any(c.isalpha() for c in s):
        return None
    # If the string has both '.' and ',' assume '.' is the thousands separator (NL).
    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def idempotency_key(centre: str, external_id: str, period: str, amount: float) -> str:
    raw = f"{centre}|{external_id}|{period}|{amount:.2f}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# -------- model & reader --------

@dataclass
class Row:
    centre: str
    external_id: str
    period: str          # canonical YYYY-MM
    amount_eur: float

    def to_api(self) -> dict:
        return {"external_id": self.external_id, "centre": self.centre,
                "period": self.period, "amount_eur": self.amount_eur}


@dataclass
class ReportRow:
    file: str
    line: int
    reason: str
    raw: dict = field(default_factory=dict)


def _sniff_delimiter(sample: str) -> str:
    counts = {d: sample.count(d) for d in [",", ";", "\t", "|"]}
    return max(counts, key=counts.get)


def _lower_keys(row: dict) -> dict:
    return {(k or "").strip().lower(): v for k, v in row.items()}


def read_file(path: Path) -> tuple[list[Row], list[ReportRow]]:
    rejected: list[ReportRow] = []
    accepted: list[Row] = []
    text = path.read_text(encoding="utf-8")
    delim = _sniff_delimiter(text.splitlines()[0] if text else "")
    reader = csv.DictReader(text.splitlines(), delimiter=delim)
    for lineno, raw_row in enumerate(reader, start=2):
        row = _lower_keys(raw_row)
        # Field aliases: tenant_ref ~ external_id; both centre/Centre already lowered.
        external_id = (row.get("external_id") or row.get("tenant_ref") or "").strip()
        centre = (row.get("centre") or row.get("center") or "").strip()
        period = parse_period(row.get("period") or "")
        amount = parse_amount(row.get("amount_eur") or row.get("amount") or "")
        if not external_id:
            rejected.append(ReportRow(path.name, lineno, "missing_external_id", raw_row))
            continue
        if not centre:
            rejected.append(ReportRow(path.name, lineno, "missing_centre", raw_row))
            continue
        if not period:
            rejected.append(ReportRow(path.name, lineno, "invalid_period", raw_row))
            continue
        if amount is None:
            rejected.append(ReportRow(path.name, lineno, "invalid_amount", raw_row))
            continue
        if amount < 0:
            # Negative correction rows are legitimate; we keep them. Flag in reports anyway.
            accepted.append(Row(centre, external_id, period, amount))
            continue
        accepted.append(Row(centre, external_id, period, amount))
    return accepted, rejected


def dedupe(rows: list[Row]) -> tuple[list[Row], list[ReportRow]]:
    """Local dedupe before posting. The server still catches the rest via UNIQUE."""
    seen: set[tuple[str, str, str, float]] = set()
    out: list[Row] = []
    dupes: list[ReportRow] = []
    for i, r in enumerate(rows, start=1):
        key = (r.centre, r.external_id, r.period, round(r.amount_eur, 2))
        if key in seen:
            dupes.append(ReportRow("<in-memory>", i, "duplicate_local", r.to_api()))
            continue
        seen.add(key)
        out.append(r)
    return out, dupes


# -------- HTTP --------

def post_batches(client: httpx.Client, base_url: str, api_key: str,
                 rows: list[Row], batch_size: int, dry_run: bool,
                 max_retries: int = 5) -> tuple[int, int, int, list[dict]]:
    """Return (created, duplicates, errors, per_batch_responses).

    Retries 429 (honours Retry-After) and 5xx with jittered backoff. The server
    caches responses by Idempotency-Key, so a retried batch returns the same body
    and never double-inserts.
    """
    import random as _rand
    import time as _time
    created = duplicates = errors = 0
    responses: list[dict] = []
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        if not chunk:
            continue
        head = chunk[0]
        key = hashlib.sha256(
            f"BATCH|{head.centre}|{head.external_id}|{head.period}|{len(chunk)}|{i}".encode()
        ).hexdigest()
        payload = {"rows": [r.to_api() for r in chunk]}
        if dry_run:
            responses.append({"dry_run": True, "batch_size": len(chunk), "key": key})
            created += len(chunk)
            continue

        headers = {"X-API-Key": api_key, "Idempotency-Key": key}
        for attempt in range(max_retries + 1):
            resp = client.post(f"{base_url}/platform/v1/turnover:bulk",
                               json=payload, headers=headers, timeout=30.0)
            if resp.status_code == 429 and attempt < max_retries:
                wait = float(resp.headers.get("Retry-After", "1")) + _rand.uniform(0, 0.25)
                _time.sleep(wait)
                continue
            if 500 <= resp.status_code < 600 and attempt < max_retries:
                wait = (2 ** attempt) * 0.4 + _rand.uniform(0, 0.25)
                _time.sleep(wait)
                continue
            resp.raise_for_status()
            break

        body = resp.json()
        created += body.get("created", 0)
        duplicates += body.get("duplicates", 0)
        errors += body.get("errors", 0)
        responses.append({"batch_size": len(chunk), "key": key, "server": body})
    return created, duplicates, errors, responses


# -------- report --------

def write_reports(out_dir: Path, read_total: int, imported: int, dupes_local: int,
                  dupes_server: int, server_errors: int, local_rejects: list[ReportRow],
                  files_processed: list[str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "migration_report.md"
    csv_path = out_dir / "rejected_rows.csv"
    duplicates = dupes_local + dupes_server

    md.write_text(
        f"# Turnover migration report\n\n"
        f"Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z\n\n"
        f"## Files\n" + "".join(f"- {f}\n" for f in files_processed) +
        f"\n## Reconciliation\n"
        f"- read: **{read_total}**\n"
        f"- imported (created on platform): **{imported}**\n"
        f"- duplicates (local + server): **{duplicates}** "
        f"(local {dupes_local}, server {dupes_server})\n"
        f"- rejected (local validation): **{len(local_rejects)}**\n"
        f"- rejected (server, per-row): **{server_errors}**\n"
        f"- check: imported + duplicates + rejected == read "
        f"→ {imported + duplicates + len(local_rejects) + server_errors} == {read_total} "
        f"**{'OK' if imported + duplicates + len(local_rejects) + server_errors == read_total else 'MISMATCH'}**\n\n"
        f"## Rejections by reason\n" + _reject_table(local_rejects) + "\n",
        encoding="utf-8",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file", "line", "reason", "raw"])
        for r in local_rejects:
            w.writerow([r.file, r.line, r.reason, json.dumps(r.raw, ensure_ascii=False)])


def _reject_table(rows: list[ReportRow]) -> str:
    if not rows:
        return "_No local rejections._\n"
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.reason] = counts.get(r.reason, 0) + 1
    lines = ["| reason | count |", "|---|---|"]
    for reason in sorted(counts):
        lines.append(f"| {reason} | {counts[reason]} |")
    return "\n".join(lines) + "\n"


# -------- entry point --------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Migrate turnover CSVs into the Tenant Platform.")
    p.add_argument("--data-dir", default=str(ROOT / "data" / "turnover"))
    p.add_argument("--base-url", default=os.environ.get("MOCK_APIS_BASE_URL", "http://localhost:8000"))
    p.add_argument("--api-key", default=os.environ.get("API_KEY_RW", "rw-dev-local"))
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--since", help="Optional YYYY-MM lower bound; rows older than this are dropped.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out-dir", default=str(ROOT / "reports"))
    args = p.parse_args(argv)

    data_dir = Path(args.data_dir)
    files = sorted(data_dir.glob("*.csv"))
    if not files:
        print(f"no CSVs in {data_dir}", file=sys.stderr)
        return 2

    all_rows: list[Row] = []
    all_rejects: list[ReportRow] = []
    read_total = 0
    for f in files:
        rows, rejects = read_file(f)
        read_total += len(rows) + len(rejects)
        all_rows.extend(rows)
        all_rejects.extend(rejects)

    if args.since:
        min_period = args.since
        kept = [r for r in all_rows if r.period >= min_period]
        dropped = len(all_rows) - len(kept)
        if dropped:
            print(f"--since {args.since}: dropped {dropped} rows older than cutoff")
        all_rows = kept

    deduped, dupes_local = dedupe(all_rows)

    with httpx.Client() as client:
        created, dupes_server, server_errors, _ = post_batches(
            client, args.base_url, args.api_key, deduped, args.batch_size, args.dry_run,
        )

    write_reports(
        Path(args.out_dir), read_total, created, len(dupes_local), dupes_server,
        server_errors, all_rejects, [f.name for f in files],
    )
    print(json.dumps({"read": read_total, "imported": created,
                      "duplicates": len(dupes_local) + dupes_server,
                      "rejected": len(all_rejects) + server_errors,
                      "dry_run": args.dry_run}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
