"""detect_anomalies: inventory risk, unusual sales movements, and data-quality exceptions."""

from __future__ import annotations

import datetime as dt

import duckdb
import pandas as pd

from stockroom.tools.base import ToolError, ToolResult, as_of, store_day_caveats, warehouse

SCOPES = ("inventory", "sales", "data_quality", "all")
OVERSTOCK_DAYS = 90  # days of cover above which stock is flagged as excess
DEAD_STOCK_UNITS = 24  # on-hand with zero recent demand that counts as dead stock


def _filters(store: str | None, dept: str | None, s_alias: str = "x", k_alias: str = "k") -> tuple[str, list]:
    where, params = [], []
    if store:
        where.append(f"{s_alias}.store = ?")
        params.append(store)
    if dept:
        where.append(f"{k_alias}.dept = ?")
        params.append(dept)
    return (" AND " + " AND ".join(where)) if where else "", params


def inventory_anomalies(
    cur: duckdb.DuckDBPyConnection, store: str | None = None, dept: str | None = None
) -> pd.DataFrame:
    """Every store-SKU with a stock-out, a stock-out risk inside supplier lead time, or excess stock."""
    f, params = _filters(store, dept, "i", "k")
    d = as_of()
    return cur.execute(
        f"""
        WITH open_days AS (
            SELECT store, count(*) AS n FROM core.store_day_status
            WHERE status = 'open' AND date > ?::DATE - 28 AND date <= ? GROUP BY store
        ), dem AS (
            SELECT s.store, s.sku, sum(s.units) AS units_28d FROM core.fact_sales_daily s
            WHERE s.date > ?::DATE - 28 AND s.date <= ? GROUP BY ALL
        ), px AS (
            SELECT store, sku, price FROM core.fact_price_weekly
            QUALIFY row_number() OVER (PARTITION BY store, sku ORDER BY wm_yr_wk DESC) = 1
        ), base AS (
            SELECT i.store, i.sku, k.dept, k.description, i.on_hand_units, i.on_order_units,
                   coalesce(dem.units_28d, 0) / o.n AS avg_daily,
                   sup.lead_time_days, k.unit_cost, px.price
            FROM core.inventory i
            JOIN core.dim_sku k USING (sku)
            JOIN core.dim_supplier sup USING (supplier_id)
            JOIN open_days o USING (store)
            LEFT JOIN dem USING (store, sku)
            LEFT JOIN px USING (store, sku)
            WHERE k.status = 'ACTIVE' {f}
        )
        SELECT * FROM (
        SELECT *,
            CASE WHEN avg_daily > 0 THEN on_hand_units / avg_daily END AS days_of_cover,
            CASE
                WHEN on_hand_units = 0 AND avg_daily > 0 THEN 'stockout'
                WHEN avg_daily > 0 AND (on_hand_units + on_order_units) / avg_daily < lead_time_days
                    THEN 'stockout_risk'
                WHEN avg_daily > 0 AND on_hand_units / avg_daily > {OVERSTOCK_DAYS} THEN 'overstock'
                WHEN avg_daily = 0 AND on_hand_units >= {DEAD_STOCK_UNITS} THEN 'dead_stock'
            END AS anomaly,
            CASE
                WHEN on_hand_units = 0 AND avg_daily > 0 THEN avg_daily * lead_time_days * price
                WHEN avg_daily > 0 AND (on_hand_units + on_order_units) / avg_daily < lead_time_days
                    THEN (avg_daily * lead_time_days - on_hand_units - on_order_units) * price
                WHEN avg_daily > 0 AND on_hand_units / avg_daily > {OVERSTOCK_DAYS}
                    THEN (on_hand_units - 30 * avg_daily) * unit_cost
                WHEN avg_daily = 0 AND on_hand_units >= {DEAD_STOCK_UNITS} THEN on_hand_units * unit_cost
            END AS value_usd
        FROM base
        ) WHERE anomaly IS NOT NULL
        ORDER BY value_usd DESC
        """,
        [d, d, d, d, *params],
    ).df()


def sales_anomalies(
    cur: duckdb.DuckDBPyConnection, end: dt.date, store: str | None = None, dept: str | None = None
) -> pd.DataFrame:
    """Store x dept weeks far from their 4-week baseline, and SKUs whose sales stopped or spiked."""
    f, params = _filters(store, dept, "s", "k")
    week = cur.execute(
        f"""
        WITH blocks AS (
            SELECT s.store, k.dept, CAST(floor((?::DATE - s.date) / 7) AS INTEGER) AS blk, sum(s.units) AS units
            FROM core.fact_sales_daily s JOIN core.dim_sku k USING (sku)
            WHERE s.date > ?::DATE - 35 AND s.date <= ? {f}
            GROUP BY ALL
        ), bad AS (  -- blocks touching a missing-data day are unusable
            SELECT DISTINCT store, CAST(floor((?::DATE - date) / 7) AS INTEGER) AS blk
            FROM core.store_day_status WHERE status = 'missing_data' AND date > ?::DATE - 35 AND date <= ?
        ), ok AS (
            SELECT b.* FROM blocks b ANTI JOIN bad USING (store, blk)
        ), stats AS (
            SELECT store, dept,
                   max(units) FILTER (WHERE blk = 0) AS this_week,
                   avg(units) FILTER (WHERE blk BETWEEN 1 AND 4) AS base_mean,
                   stddev_samp(units) FILTER (WHERE blk BETWEEN 1 AND 4) AS base_sd,
                   count(*) FILTER (WHERE blk BETWEEN 1 AND 4) AS base_n
            FROM ok GROUP BY ALL
        )
        SELECT store, dept AS subject, 'store_dept_week' AS level,
               CASE WHEN this_week > base_mean THEN 'sales_spike' ELSE 'sales_drop' END AS anomaly,
               this_week, base_mean, (this_week - base_mean) / base_mean AS pct_change,
               (this_week - base_mean) / nullif(base_sd, 0) AS z
        FROM stats
        WHERE base_n >= 3 AND this_week IS NOT NULL
          AND abs(this_week - base_mean) / base_mean > 0.25
          AND abs((this_week - base_mean) / nullif(base_sd, 0)) > 2
        """,
        [end, end, end, *params, end, end, end],
    ).df()
    sku = cur.execute(
        f"""
        WITH w AS (
            SELECT s.store, s.sku, k.dept,
                   sum(s.units) FILTER (WHERE s.date > ?::DATE - 14) AS last14,
                   sum(s.units) FILTER (WHERE s.date <= ?::DATE - 14 AND s.date > ?::DATE - 42) / 28.0 AS base_daily,
                   sum(s.units) FILTER (WHERE s.date > ?::DATE - 7) AS last7
            FROM core.fact_sales_daily s JOIN core.dim_sku k USING (sku)
            WHERE s.date > ?::DATE - 42 AND s.date <= ? {f}
            GROUP BY ALL
        )
        SELECT store, sku AS subject, 'sku' AS level,
               CASE WHEN coalesce(last14, 0) = 0 THEN 'sales_stopped' ELSE 'sku_spike' END AS anomaly,
               coalesce(last7, 0) AS this_week, base_daily * 7 AS base_mean,
               (coalesce(last7, 0) - base_daily * 7) / (base_daily * 7) AS pct_change, NULL AS z
        FROM w
        WHERE (coalesce(last14, 0) = 0 AND base_daily >= 1.5)
           OR (coalesce(last7, 0) >= 20 AND coalesce(last7, 0) > 4 * base_daily * 7)
        """,
        [end, end, end, end, end, end, *params],
    ).df()
    out = pd.concat([week, sku], ignore_index=True)
    if out.empty:
        return out
    return out.reindex(out["pct_change"].abs().sort_values(ascending=False).index).reset_index(drop=True)


def detect_anomalies(
    scope: str = "all",
    store: str | None = None,
    dept: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
) -> ToolResult:
    """Find stock-outs, stock-out risk, excess stock, unusual sales, and data-quality exceptions."""
    if scope not in SCOPES:
        raise ToolError(f"scope must be one of {SCOPES}")
    end = dt.date.fromisoformat(end_date) if end_date else as_of()
    if end > as_of():
        raise ToolError(f"end_date {end} is after the as-of date {as_of()}; there is no data then.")
    limit = max(1, min(int(limit), 50))
    cur = warehouse().cursor()
    data: dict = {"end_date": end}
    caveats: list[str] = []

    if scope in ("inventory", "all"):
        if end != as_of():
            caveats.append(f"Inventory exists only as a snapshot on {as_of()}; inventory checks skipped.")
        else:
            inv = inventory_anomalies(cur, store, dept)
            data["inventory"] = {
                "counts": inv["anomaly"].value_counts().to_dict(),
                "value_usd_by_type": inv.groupby("anomaly")["value_usd"].sum().round(0).to_dict(),
                "top": inv.head(limit)[
                    [
                        "store",
                        "sku",
                        "dept",
                        "anomaly",
                        "on_hand_units",
                        "on_order_units",
                        "avg_daily",
                        "days_of_cover",
                        "lead_time_days",
                        "value_usd",
                    ]
                ].to_dict(orient="records"),
                "value_definition": "stockout: revenue at risk over supplier lead time; stockout_risk: revenue "
                "of the projected shortfall; overstock: cost of stock beyond 30 days of cover; dead_stock: "
                "cost of stock with no sales in 28 days",
            }
    if scope in ("sales", "all"):
        sal = sales_anomalies(cur, end, store, dept)
        data["sales"] = {
            "window": f"7 days ending {end} vs the 4 prior weeks; SKU checks use 14 days vs the prior 28",
            "counts": sal["anomaly"].value_counts().to_dict() if not sal.empty else {},
            "top": sal.head(limit).to_dict(orient="records"),
        }
        caveats.append("'sales_stopped' can mean a stock-out, a delisting or a code change; check inventory.")
    if scope in ("data_quality", "all"):
        dq = cur.execute("SELECT issue_type, rows_affected, finding FROM core.dq_issues").df()
        data["data_quality"] = dq.to_dict(orient="records")
    caveats += store_day_caveats(cur, end - dt.timedelta(days=42), end, store)
    return ToolResult(
        data=data,
        caveats=caveats,
        provenance={
            "tables": ["core.inventory", "core.fact_sales_daily", "core.store_day_status", "core.dq_issues"]
        },
    )
