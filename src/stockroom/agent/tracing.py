"""Every agent turn is recorded twice: a row in app.duckdb (queryable, feeds the UI) and a JSONL line
under runs/ (greppable, survives a DB rebuild). Evals read the same records, so what the demo shows,
what we measure, and what we debug are one artefact.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from stockroom import config
from stockroom.appdb import app_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    conversation_id VARCHAR, turn INTEGER, started_at TIMESTAMP, model VARCHAR, effort VARCHAR,
    question VARCHAR, answer VARCHAR, stop_reason VARCHAR, tool_calls INTEGER, steps VARCHAR,
    usage VARCHAR, cost_usd DOUBLE, conversation_cost_usd DOUBLE, latency_s DOUBLE, limits_hit VARCHAR
);
"""


@dataclass
class Step:
    """One tool call: what the model asked for and what came back."""

    tool: str
    input: dict[str, Any]
    output: dict[str, Any]
    ms: float
    is_error: bool


@dataclass
class TurnTrace:
    conversation_id: str
    turn: int
    started_at: dt.datetime
    model: str
    effort: str
    question: str
    answer: str = ""
    stop_reason: str = ""
    steps: list[Step] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    conversation_cost_usd: float = 0.0
    latency_s: float = 0.0
    limits_hit: list[str] = field(default_factory=list)

    @property
    def tool_calls(self) -> int:
        return len(self.steps)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat()
        return d


def record(t: TurnTrace, jsonl: bool = True) -> None:
    cur = app_db().cursor()
    cur.execute(SCHEMA)
    cur.execute(
        "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            t.conversation_id, t.turn, t.started_at, t.model, t.effort, t.question, t.answer,
            t.stop_reason, t.tool_calls, json.dumps([asdict(s) for s in t.steps], default=str),
            json.dumps(t.usage), t.cost_usd, t.conversation_cost_usd, t.latency_s, json.dumps(t.limits_hit),
        ],
    )  # fmt: skip
    if jsonl:
        path = config.RUNS_DIR / "traces" / f"{t.started_at:%Y-%m-%d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(t.to_json(), default=str) + "\n")
