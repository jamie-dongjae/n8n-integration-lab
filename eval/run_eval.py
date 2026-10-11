"""Eval harness for the WF2 triage classifier.

Two modes:
- `--mode local`: run the Python port of the rule-based triage. Works offline.
  Great for CI and for anyone who clones the repo without n8n running.
- `--mode webhook`: POST each case to `${N8N_BASE_URL}/webhook/triage`.
  Use this when n8n is up (real Claude or mock) to prove the whole stack end-to-end.

Writes `reports/eval.md` with the headline accuracy number, a confusion matrix,
and the full per-case table so misses are inspectable, not hidden.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.rules import classify as classify_local  # noqa: E402

CATEGORIES = ["auth", "validation", "mapping", "rate_limit", "upstream_outage", "duplicate", "unknown"]


def load_cases(path: Path) -> list[dict]:
    cases = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise SystemExit(f"eval cases: bad JSON on line {lineno}: {e}")
    return cases


def predict_local(record: dict) -> dict:
    return classify_local(record)


def predict_webhook(client: httpx.Client, base_url: str, record: dict) -> dict:
    r = client.post(f"{base_url}/webhook/triage", json=record, timeout=60.0)
    r.raise_for_status()
    out = r.json()
    return out.get("triage") or out


def confusion(predictions: list[tuple[str, str]]) -> dict[str, dict[str, int]]:
    """Return a dict[expected][predicted] -> count."""
    m: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for exp, pred in predictions:
        m[exp][pred] += 1
    return m


def format_markdown(mode: str, cases: list[dict], results: list[dict]) -> str:
    hits = sum(1 for r in results if r["predicted"] == r["expected"])
    total = len(results)
    acc = hits / total if total else 0.0
    exp_counts = Counter(r["expected"] for r in results)
    conf = confusion([(r["expected"], r["predicted"]) for r in results])

    md: list[str] = []
    md.append("# Triage eval\n")
    md.append(f"Mode: `{mode}`  \n")
    md.append(f"Cases: **{total}** · correct: **{hits}** · accuracy: **{acc:.0%}**\n")
    md.append("## Per-category accuracy\n")
    md.append("| category | n | correct | accuracy |")
    md.append("|---|---|---|---|")
    for cat in CATEGORIES:
        n = exp_counts.get(cat, 0)
        correct = conf.get(cat, {}).get(cat, 0)
        pct = f"{(correct/n):.0%}" if n else "–"
        md.append(f"| {cat} | {n} | {correct} | {pct} |")
    md.append("\n## Confusion matrix (expected → predicted)\n")
    md.append("| expected \\ predicted | " + " | ".join(CATEGORIES) + " |")
    md.append("|" + "|".join(["---"] * (len(CATEGORIES) + 1)) + "|")
    for exp in CATEGORIES:
        row = [exp]
        for pred in CATEGORIES:
            row.append(str(conf.get(exp, {}).get(pred, 0)))
        md.append("| " + " | ".join(row) + " |")
    md.append("\n## Per-case\n")
    md.append("| id | expected | predicted | confidence | ok |")
    md.append("|---|---|---|---|---|")
    for r in results:
        ok = "✓" if r["predicted"] == r["expected"] else "✗"
        md.append(f"| {r['id']} | {r['expected']} | {r['predicted']} | {r.get('confidence', 0):.2f} | {ok} |")
    md.append("")
    return "\n".join(md)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["local", "webhook"], default="local")
    p.add_argument("--cases", default=str(ROOT / "eval" / "triage_cases.jsonl"))
    p.add_argument("--n8n-base", default=os.environ.get("N8N_BASE_URL", "http://localhost:5678"))
    p.add_argument("--out", default=str(ROOT / "reports" / "eval.md"))
    args = p.parse_args(argv)

    cases = load_cases(Path(args.cases))
    if not cases:
        print("no cases loaded", file=sys.stderr)
        return 2

    results: list[dict] = []
    with httpx.Client() as client:
        for case in cases:
            if args.mode == "local":
                triage = predict_local(case["input"])
            else:
                triage = predict_webhook(client, args.n8n_base, case["input"])
            results.append({
                "id": case["id"],
                "expected": case["expected"],
                "predicted": triage.get("category", "unknown"),
                "confidence": float(triage.get("confidence", 0) or 0),
                "model": triage.get("model"),
            })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(format_markdown(args.mode, cases, results), encoding="utf-8")

    hits = sum(1 for r in results if r["predicted"] == r["expected"])
    print(json.dumps({"mode": args.mode, "cases": len(results), "correct": hits,
                      "accuracy": hits / len(results)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
