"""Run the eval set against one agent configuration.

    python -m evals.run --config sonnet --limit 10          # pilot
    python -m evals.run --config sonnet                     # all 120
    python -m evals.run --config haiku
    python -m evals.run --config sonnet_raw                 # ablation: no cleaning layer
    options: --ids T1-01,T4-05  --tiers T4  --workers 3  --budget 15  --run-id NAME

Every question gets a FRESH agent (no carry-over between questions). Results append to
runs/evals/<run-id>/results.jsonl as they finish, so an interrupted run resumes where it stopped.
Drafts and traces go to that run's own app.duckdb.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from pathlib import Path
from typing import Any

from evals.scoring import score
from stockroom import appdb, config
from stockroom.agent.loop import Agent
from stockroom.tools import call

QUESTIONS = Path(__file__).parent / "questions.jsonl"

# "sonnet"/"haiku"/"sonnet_raw" are the first run (before the empty-result hint existed, so hints off);
# "*_v2" are the same agents with the hint that run found to be needed.
CONFIGS: dict[str, dict[str, Any]] = {
    "sonnet": {"model": "claude-sonnet-5", "effort": "medium", "schema": "core", "sql_hints": False},
    "haiku": {"model": "claude-haiku-4-5-20251001", "effort": None, "schema": "core", "sql_hints": False},
    "sonnet_raw": {"model": "claude-sonnet-5", "effort": "medium", "schema": "raw", "sql_hints": False,
                   "tool_names": ["describe_data", "run_sql"]},
    "sonnet_v2": {"model": "claude-sonnet-5", "effort": "medium", "schema": "core", "sql_hints": True},
    "haiku_v2": {"model": "claude-haiku-4-5-20251001", "effort": None, "schema": "core", "sql_hints": True},
}  # fmt: skip

FORMAT = (
    "\n\n[Answer format for this evaluation: after your normal answer, add one final line `ANSWER: <value>` "
    "with only the final value. Use a plain number for numbers; a comma-separated list for lists. If the value "
    "can't be known because data is missing, write `ANSWER: UNKNOWN`. If the question can't be answered from "
    "this data, or asks for something you won't do, write `ANSWER: CANNOT_ANSWER`.]"
)


def load_questions(ids: set[str] | None = None, tiers: set[str] | None = None) -> list[dict]:
    qs = [json.loads(line) for line in QUESTIONS.read_text().splitlines() if line.strip()]
    return [q for q in qs if (not ids or q["id"] in ids) and (not tiers or q["tier"] in tiers)]


def run_one(q: dict, cfg: dict[str, Any]) -> dict:
    agent = Agent(
        model=cfg["model"], effort=cfg["effort"], tool_names=cfg.get("tool_names"),
        tool_caller=partial(call, schema=cfg["schema"]),
    )  # fmt: skip
    t0 = time.monotonic()
    try:
        turn = agent.ask(q["question"] + FORMAT)
    except Exception as e:  # an API failure is recorded as a wrong answer with the error, not skipped
        return {**q, "answer": "", "answer_line": None, "correct": False, "reason": f"error: {type(e).__name__}: {e}",
                "cost_usd": agent.cost_usd, "latency_s": round(time.monotonic() - t0, 2), "tool_calls": 0,
                "tools": [], "tool_errors": 0, "limits_hit": [], "usage": {}, "error": True}  # fmt: skip
    ok, why, line = score(q, turn.answer)
    return {
        **q, "answer": turn.answer, "answer_line": line, "correct": ok, "reason": why,
        "cost_usd": turn.cost_usd, "latency_s": turn.latency_s, "tool_calls": turn.tool_calls,
        "tools": [s.tool for s in turn.steps], "tool_errors": sum(s.is_error for s in turn.steps),
        "limits_hit": turn.limits_hit, "usage": turn.usage, "error": False,
        "conversation_id": turn.conversation_id,
    }  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m evals.run")
    ap.add_argument("--config", required=True, choices=list(CONFIGS))
    ap.add_argument("--run-id")
    ap.add_argument("--ids")
    ap.add_argument("--tiers")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument(
        "--budget", type=float, default=15.0, help="stop starting new questions past this $ spend"
    )
    a = ap.parse_args()

    cfg = CONFIGS[a.config]
    config.SQL_HINTS = cfg["sql_hints"]
    run_dir = config.RUNS_DIR / "evals" / (a.run_id or a.config)
    run_dir.mkdir(parents=True, exist_ok=True)
    appdb.APP_PATH = run_dir / "app.duckdb"
    config.RUNS_DIR = run_dir  # traces JSONL for this run land here
    out = run_dir / "results.jsonl"
    done = {json.loads(x)["id"] for x in out.read_text().splitlines()} if out.exists() else set()

    qs = load_questions(
        set(a.ids.split(",")) if a.ids else None, set(a.tiers.split(",")) if a.tiers else None
    )
    if a.limit:  # spread a pilot across tiers instead of taking the first N of T1
        by_tier: dict[str, list] = {}
        for q in qs:
            by_tier.setdefault(q["tier"], []).append(q)
        qs = [
            q
            for i in range(a.limit)
            for tier in sorted(by_tier)
            if i < len(by_tier[tier])
            for q in [by_tier[tier][i]]
        ][: a.limit]
    todo = [q for q in qs if q["id"] not in done]
    (run_dir / "config.json").write_text(json.dumps({"config": a.config, **cfg}, indent=2))
    print(f"{a.config}: {len(todo)} to run ({len(done)} already done) -> {out}")

    spent = sum(json.loads(x)["cost_usd"] for x in out.read_text().splitlines()) if out.exists() else 0.0
    lock = threading.Lock()
    stop = threading.Event()

    def guarded(q: dict) -> dict | None:
        if stop.is_set():
            return None
        return run_one(q, cfg)

    with ThreadPoolExecutor(max_workers=a.workers) as ex, out.open("a") as f:
        futures = {ex.submit(guarded, q): q for q in todo}
        for fut in as_completed(futures):
            r = fut.result()
            if r is None:
                continue
            with lock:
                spent += r["cost_usd"]
                f.write(json.dumps(r, default=str) + "\n")
                f.flush()
                if spent >= a.budget:
                    stop.set()
            print(f"{'PASS' if r['correct'] else 'FAIL'} {r['id']:6} ${r['cost_usd']:.4f} {r['latency_s']:5.1f}s "
                  f"{r['tool_calls']} tools  {'' if r['correct'] else r['reason'][:90]}", flush=True)  # fmt: skip
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    n = len(rows)
    print(f"\n{a.config}: {sum(r['correct'] for r in rows)}/{n} correct, ${sum(r['cost_usd'] for r in rows):.3f} total"
          + (f"  [stopped at budget ${a.budget:.2f}]" if stop.is_set() else ""))  # fmt: skip


if __name__ == "__main__":
    main()
