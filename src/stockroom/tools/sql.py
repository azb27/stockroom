"""describe_data and run_sql: the two tools that let the model explore the cleaned warehouse."""

from __future__ import annotations

import threading
import time

import sqlglot
from sqlglot import exp

from stockroom import config
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


RAW_SEMANTICS = [
    "Today (as-of date) is {as_of}. Data covers {start} to {end}.",
    "These are the ERP and POS exports exactly as the customer delivered them. Nothing has been cleaned.",
    (
        "raw.pos_sales_lines has one row per POS line: qty in the unit given by the uom column; load_batch_id links "
        "to raw.load_log. raw.price_book has weekly shelf prices by store and SKU (wm_yr_wk joins raw.calendar)."
    ),
    "raw.erp_sku_master holds SKU attributes (case_pack, unit_cost, supplier_id); raw.erp_suppliers has lead times.",
    "raw.inventory_snapshot has on-hand and on-order stock on the as-of date.",
]


def describe_raw() -> ToolResult:
    """Eval-only (raw-data ablation): what the customer handed over, with no cleaning layer."""
    cur = warehouse().cursor()
    cols = cur.execute(
        """SELECT table_name, column_name, data_type FROM information_schema.columns
           WHERE table_schema = 'raw' ORDER BY table_name, ordinal_position"""
    ).fetchall()
    tables: dict[str, dict] = {}
    for t, c, typ in cols:
        tables.setdefault(t, {"columns": {}})["columns"][c] = typ
    for t, meta in tables.items():
        meta["rows"] = cur.execute(f"SELECT count(*) FROM raw.{t}").fetchone()[0]
    start, end = cur.execute("SELECT min(date), max(date) FROM raw.calendar").fetchone()
    return ToolResult(
        data={
            "as_of_date": as_of(),
            "tables": {f"raw.{k}": v for k, v in tables.items()},
            "semantics": [s.format(as_of=as_of(), start=start, end=end) for s in RAW_SEMANTICS],
        },
        provenance={"tables": ["information_schema.columns"]},
    )


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


def _looks_empty(rows: list[tuple]) -> bool:
    return not rows or (len(rows) == 1 and all(v in (0, None) for v in rows[0]))


def filter_hints(cur, sql: str, schemas: frozenset[str], limit: int = 3) -> list[str]:
    """When a query comes back empty (or a lone 0), check its `column = 'text'` filters against the data.

    Found by the P5 eval: agents wrote status = 'active' (data says 'ACTIVE'), got nothing back, and
    confidently answered 0. A hint turns a silent wrong answer into a visible, fixable one.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception:
        return []
    preds: list[tuple[str, str]] = []
    for node in tree.find_all(exp.EQ, exp.In):
        col = node.find(exp.Column)
        lits = [lit for lit in node.find_all(exp.Literal) if lit.is_string]
        if col is not None and lits:
            preds += [(col.name.lower(), lit.this) for lit in lits]
    if not preds:
        return []
    referenced = {t.name.lower() for t in tree.find_all(exp.Table)}
    hints: list[str] = []
    for col, val in dict.fromkeys(preds):  # de-duplicate, keep order
        tables = cur.execute(
            "SELECT table_schema, table_name FROM information_schema.columns "
            "WHERE column_name = ? AND table_schema IN (SELECT unnest(?)) ORDER BY table_name",
            [col, sorted(schemas)],
        ).fetchall()
        tables = [t for t in tables if t[1].lower() in referenced] or tables[:1]
        for schema, table in tables[:1]:
            fq = f"{schema}.{table}"
            if cur.execute(f'SELECT 1 FROM {fq} WHERE "{col}" = ? LIMIT 1', [val]).fetchone():
                continue
            alt = cur.execute(
                f'SELECT DISTINCT CAST("{col}" AS VARCHAR) FROM {fq} WHERE lower(CAST("{col}" AS VARCHAR)) = lower(?) LIMIT 1',
                [val],
            ).fetchone()
            if alt:
                hints.append(f"No rows in {fq} have {col} = '{val}'. Text comparisons are case-sensitive: "
                             f"the data uses '{alt[0]}'. Re-run with that value before concluding the answer is 0.")  # fmt: skip
            else:
                n = cur.execute(f'SELECT count(DISTINCT "{col}") FROM {fq}').fetchone()[0]
                sample = ""
                if n <= 20:
                    vals = [
                        r[0]
                        for r in cur.execute(
                            f'SELECT DISTINCT CAST("{col}" AS VARCHAR) FROM {fq} ORDER BY 1'
                        ).fetchall()
                    ]
                    sample = f" Valid values: {', '.join(map(str, vals))}."
                hints.append(
                    f"No rows in {fq} have {col} = '{val}'.{sample} Check the filter before concluding the answer is 0."
                )
        if len(hints) >= limit:
            break
    return hints


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
    if _looks_empty(rows) and config.SQL_HINTS:
        try:
            caveats += filter_hints(cur, safe, allowed_schemas)
        except Exception:  # a hint must never break the query result
            pass
    return ToolResult(
        data={"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated},
        caveats=caveats,
        provenance={"sql": safe, "elapsed_ms": round(elapsed_ms, 1)},
    )
