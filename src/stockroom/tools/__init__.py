"""Tool registry: one definition per tool, shared by the agent loop (P4) and the MCP server (P6).

`call(name, args)` is the only entry point adapters should use. It validates argument names
against the schema, so a model cannot pass parameters the schema doesn't declare (e.g.
`allowed_schemas`), and turns ToolError into an {"error": ...} payload the model can act on.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from stockroom.tools.anomalies import SCOPES, detect_anomalies
from stockroom.tools.base import ToolError, ToolResult, jsonable
from stockroom.tools.sql import MAX_ROWS, describe_data, run_sql
from stockroom.tools.variance import GROUPS, explain_variance

DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$", "description": "YYYY-MM-DD"}
STORE = {"type": "string", "enum": ["CA_1", "CA_2", "CA_3", "CA_4"]}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., ToolResult]

    def anthropic_spec(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "describe_data",
            "Start here. Returns the cleaned warehouse's tables and columns, how to interpret them "
            "(units, revenue, weeks, what a missing row means), the as-of date, the data-quality log, and "
            "store-days with missing data. Call once per conversation before writing SQL.",
            _schema({}),
            describe_data,
        ),
        Tool(
            "run_sql",
            f"Run ONE read-only DuckDB SELECT over core.* tables (e.g. core.sales_enriched for units and revenue). "
            f"Returns at most {MAX_ROWS} rows, so aggregate in SQL. Rejected: writes, raw.* tables, table functions, "
            "multiple statements. Read the returned caveats and repeat any that affect your answer.",
            _schema(
                {
                    "query": {"type": "string", "description": "A single SELECT (CTEs allowed)."},
                    "max_rows": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
                },
                ["query"],
            ),
            run_sql,
        ),
        Tool(
            "detect_anomalies",
            "Find problems worth acting on. inventory: stock-outs, stock-out risk within supplier lead time, "
            "overstock and dead stock, ranked by $ impact (snapshot on the as-of date only). sales: store x dept "
            "weeks far from their 4-week baseline, and SKUs whose sales stopped or spiked. data_quality: issues "
            "found and fixed while cleaning the data.",
            _schema(
                {
                    "scope": {"type": "string", "enum": list(SCOPES)},
                    "store": STORE,
                    "dept": {"type": "string", "description": "e.g. FOODS_3, HOUSEHOLD_1, HOBBIES_2"},
                    "end_date": {**DATE, "description": "Last day of the window (default: as-of date)."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
            ),
            detect_anomalies,
        ),
        Tool(
            "explain_variance",
            "Explain a revenue change between two periods: split the delta into volume (more or fewer units), "
            "mix (shift toward pricier or cheaper SKUs) and price (same SKUs, different prices), per group. "
            "Use equal-length periods, ideally the same weekdays.",
            _schema(
                {
                    "period_a_start": DATE,
                    "period_a_end": DATE,
                    "period_b_start": DATE,
                    "period_b_end": DATE,
                    "by": {"type": "string", "enum": list(GROUPS)},
                    "store": STORE,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                ["period_a_start", "period_a_end", "period_b_start", "period_b_end"],
            ),
            explain_variance,
        ),
    ]
}


def call(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a tool by name. Always returns a JSON-safe dict; errors come back as {"error": ...}."""
    args = args or {}
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}; available: {sorted(TOOLS)}"}
    allowed = set(tool.input_schema["properties"])
    unknown = set(args) - allowed
    if unknown:
        return {"error": f"unknown argument(s) {sorted(unknown)} for {name}; allowed: {sorted(allowed)}"}
    missing = set(tool.input_schema["required"]) - set(args)
    if missing:
        return {"error": f"missing required argument(s) {sorted(missing)} for {name}"}
    t0 = time.perf_counter()
    try:
        out = tool.fn(**args).to_dict()
    except ToolError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"internal error in {name}: {type(e).__name__}: {str(e).splitlines()[0][:200]}"}
    out.setdefault("provenance", {})["tool_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return jsonable(out)


__all__ = ["TOOLS", "Tool", "ToolError", "ToolResult", "call"]
