"""describe_data and run_sql: the two tools that let the model explore the cleaned warehouse."""

from __future__ import annotations

import threading
import time

from stockroom.tools import sql_guard
from stockroom.tools.base import ToolError, ToolResult, as_of, store_day_caveats, warehouse

MAX_ROWS = 200
TIMEOUT_S = 5.0

SEMANTICS = [
    "Today (as-of date) is {as_of}. Data covers {start} to {end}. Nothing after the as-of date exists.",
    "Units are eaches. Sales logged in cases were already converted.",
    "Revenue (USD) = units x that store's shelf price for that week. Use core.sales_enriched for revenue.",
    "Weeks are Walmart weeks (wm_yr_wk, Saturday-Friday). core.dim_date maps date -> wm_yr_wk and events.",
    (
        "No row in core.fact_sales_daily for a store/SKU/day means zero sales, EXCEPT store-days whose "
        "core.store_day_status.status = 'missing_data': those are unknown, never zero."
    ),
    "SKU codes are canonical, like FOODS_3_090. Legacy/alias codes (FOODS-3-090, foods_3_090) were merged.",
    (
        "Stores are retail accounts CA_1..CA_4. Inventory (core.inventory) is a snapshot on the as-of date, "
        "in eaches. Supplier lead times are in core.dim_supplier (join via core.dim_sku.supplier_id)."
    ),
    "Prices with price_is_imputed = true were forward-filled from the previous week.",
]


def describe_data() -> ToolResult:
    """Schemas, semantics, data-quality log and store-day exceptions for the cleaned warehouse."""
    cur = warehouse().cursor()
    cols = cur.execute(
        """SELECT table_name, column_name, data_type FROM information_schema.columns
           WHERE table_schema = 'core' ORDER BY table_name, ordinal_position"""
    ).fetchall()
    tables: dict[str, dict] = {}
    for t, c, typ in cols:
        tables.setdefault(t, {"columns": {}})["columns"][c] = typ
    for t, meta in tables.items():
        meta["rows"] = cur.execute(f"SELECT count(*) FROM core.{t}").fetchone()[0]
    start, end = cur.execute("SELECT min(date), max(date) FROM core.dim_date").fetchone()
    dq = cur.execute("SELECT issue_type, rows_affected, finding, fix_applied FROM core.dq_issues").fetchall()
    return ToolResult(
        data={
            "as_of_date": as_of(),
            "tables": {f"core.{k}": v for k, v in tables.items()},
            "semantics": [s.format(as_of=as_of(), start=start, end=end) for s in SEMANTICS],
            "data_quality_log": [
                {"issue": i, "rows_affected": n, "finding": f, "fix": x} for i, n, f, x in dq
            ],
        },
        caveats=store_day_caveats(cur),
        provenance={"tables": ["information_schema.columns", "core.dq_issues", "core.store_day_status"]},
    )


def run_sql(
    query: str, max_rows: int = MAX_ROWS, allowed_schemas: frozenset[str] = frozenset({"core"})
) -> ToolResult:
    """Run one read-only SELECT against core.* tables. Returns at most `max_rows` rows."""
    try:
        safe = sql_guard.check(query, allowed_schemas)
    except sql_guard.GuardError as e:
        raise ToolError(f"Query rejected: {e}") from None
    max_rows = max(1, min(int(max_rows), MAX_ROWS))

    cur = warehouse().cursor()
    timer = threading.Timer(TIMEOUT_S, cur.interrupt)
    t0 = time.perf_counter()
    timer.start()
    try:
        rel = cur.execute(f"SELECT * FROM ({safe}) AS _q LIMIT {max_rows + 1}")
        columns = [d[0] for d in rel.description]
        rows = rel.fetchall()
    except Exception as e:
        if "interrupt" in str(e).lower():
            raise ToolError(f"Query exceeded {TIMEOUT_S:.0f}s. Aggregate more or filter first.") from None
        raise ToolError(f"SQL error: {str(e).splitlines()[0][:300]}") from None
    finally:
        timer.cancel()
    elapsed_ms = (time.perf_counter() - t0) * 1000

    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    caveats: list[str] = []
    if truncated:
        caveats.append(f"Result truncated to {max_rows} rows. Aggregate or add LIMIT/ORDER BY.")
    low = safe.lower()
    if any(t in low for t in ("fact_sales_daily", "sales_enriched", "store_day_status")):
        caveats += store_day_caveats(cur)
    return ToolResult(
        data={"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated},
        caveats=caveats,
        provenance={"sql": safe, "elapsed_ms": round(elapsed_ms, 1)},
    )
