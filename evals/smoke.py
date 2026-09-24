"""CI smoke eval: 20 questions (5 per tier) that don't need the trained forecast model.

    python -m evals.smoke ids                    # print the smoke question ids (comma-separated)
    python -m evals.smoke check ci_smoke         # compare runs/evals/ci_smoke with evals/baseline.json
    python -m evals.smoke baseline haiku_v2      # write evals/baseline.json from a full run

Fails (exit 1) if smoke accuracy is more than 10 percentage points below the baseline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from evals.run import load_questions
from stockroom import config

BASELINE = Path(__file__).parent / "baseline.json"
NEEDS_MODEL = {"forecast_total", "reorder_cases", "horizon"}  # need forecast.duckdb, which CI doesn't train
MAX_DROP_PP = 10.0


def smoke_ids() -> list[str]:
    """Deterministic: per tier, the first question of each kind in id order, until 5 per tier."""
    out = []
    for tier in ("T1", "T2", "T3", "T4"):
        qs = [q for q in load_questions(tiers={tier}) if q["kind"] not in NEEDS_MODEL]
        seen, picked = set(), []
        for q in qs:  # one per kind first, then fill
            if q["kind"] not in seen:
                seen.add(q["kind"])
                picked.append(q["id"])
        for q in qs:
            if q["id"] not in picked:
                picked.append(q["id"])
        out += picked[:5]
    return out


def accuracy(run: str, ids: list[str]) -> tuple[float, int]:
    rows = [
        json.loads(x) for x in (config.RUNS_DIR / "evals" / run / "results.jsonl").read_text().splitlines()
    ]
    got = [r for r in rows if r["id"] in set(ids)]
    return 100.0 * sum(r["correct"] for r in got) / max(len(got), 1), len(got)


def main() -> None:
    cmd = sys.argv[1]
    ids = smoke_ids()
    if cmd == "ids":
        print(",".join(ids))
    elif cmd == "baseline":
        acc, n = accuracy(sys.argv[2], ids)
        BASELINE.write_text(
            json.dumps({"source_run": sys.argv[2], "ids": ids, "accuracy_pct": acc, "n": n}, indent=2) + "\n"
        )
        print(f"baseline {acc:.1f}% on {n} smoke questions from {sys.argv[2]}")
    elif cmd == "check":
        base = json.loads(BASELINE.read_text())
        acc, n = accuracy(sys.argv[2], ids)
        print(f"smoke {acc:.1f}% on {n} questions; baseline {base['accuracy_pct']:.1f}%")
        if n < len(ids) or acc < base["accuracy_pct"] - MAX_DROP_PP:
            print(f"FAIL: more than {MAX_DROP_PP:.0f} pp below baseline, or questions missing")
            sys.exit(1)


if __name__ == "__main__":
    main()
