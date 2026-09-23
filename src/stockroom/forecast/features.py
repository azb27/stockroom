"""Feature frame for the demand model, built in DuckDB from the cleaned warehouse (core.* only).

Design: a *direct* model. Every demand feature looks back at least HORIZON_DAYS (28) days, so one
model predicts any day in the next 28 without feeding its own predictions back in (no error
compounding), and a backtest from origin `o` can never see data after `o` (tested).

Prices after `price_cutoff` are frozen at the last known price: at forecast time we don't know
future shelf prices, so the backtest must not either.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import numpy as np
import pandas as pd

from stockroom.config import HORIZON_DAYS

LAG = HORIZON_DAYS  # minimum look-back of every demand feature

CATEGORICAL = ["store_i", "dept_i", "cat_i", "sku_i", "event_i"]
FEATURES = [
    "lag_28",
    "lag_35",
    "lag_42",
    "lag_49",
    "rm7",
    "rm28",
    "rm56",
    "rsd28",
    "zero28",
    "price",
    "price_rel_8w",
    "price_chg_1w",
    "dow",
    "dom",
    "month",
    "snap",
    "is_event",
    "age_days",
    *CATEGORICAL,
]

EVENT_TYPES = ["none", "Cultural", "National", "Religious", "Sporting"]


def build_frame(
    con: duckdb.DuckDBPyConnection,
    start: dt.date,
    end: dt.date,
    price_cutoff: dt.date,
    series: list[tuple[str, str]] | None = None,
) -> pd.DataFrame:
    """Features and target (`units`, NULL if unknown) for every store-SKU-day in [start, end].

    Dates after the last history date come from core.dim_date_future (target is NULL there).
    Store-days flagged missing_data have a NULL target and are ignored by the rolling windows.
    """
    lo = start - dt.timedelta(days=LAG + 56 + 7)  # enough history for the longest window
    ser_filter = ""
    params: list = []
    if series:
        con.register("_ser", pd.DataFrame(series, columns=["store", "sku"]))
        ser_filter = "AND (s.store, s.sku) IN (SELECT (store, sku) FROM _ser)"
    q = f"""
    WITH cal AS (
        SELECT date, wm_yr_wk, snap, event_type_1 FROM core.dim_date
        UNION ALL
        SELECT date, wm_yr_wk, snap, event_type_1 FROM core.dim_date_future
    ), series AS (
        SELECT s.store, s.sku, min(s.date) AS first_sale
        FROM core.fact_sales_daily s WHERE TRUE {ser_filter} GROUP BY ALL
    ), grid AS (
        SELECT se.store, se.sku, se.first_sale, c.date, c.wm_yr_wk, c.snap, c.event_type_1
        FROM series se JOIN cal c ON c.date >= greatest(se.first_sale, ?::DATE) AND c.date <= ?
    ), y AS (
        SELECT g.*,
               CASE WHEN sd.status = 'missing_data' OR sd.status IS NULL THEN NULL
                    ELSE coalesce(f.units, 0) END::FLOAT AS units
        FROM grid g
        LEFT JOIN core.fact_sales_daily f USING (store, sku, date)
        LEFT JOIN core.store_day_status sd USING (store, date)
    ), px_week AS (
        SELECT store, sku, wm_yr_wk, price FROM core.fact_price_weekly
    ), cutoff_wk AS (
        SELECT wm_yr_wk FROM core.dim_date WHERE date = ?
    ), px AS (  -- effective price: actual up to the cutoff week, frozen after it
        SELECT y.*, coalesce(p.price, last_value(p.price IGNORE NULLS) OVER w_hist) AS price
        FROM y LEFT JOIN px_week p
          ON p.store = y.store AND p.sku = y.sku
         AND p.wm_yr_wk = least(y.wm_yr_wk, (SELECT wm_yr_wk FROM cutoff_wk))
        WINDOW w_hist AS (PARTITION BY y.store, y.sku ORDER BY y.date
                          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
    )
    SELECT store, sku, date, units,
        lag(units, {LAG})      OVER w AS lag_28,
        lag(units, {LAG + 7})  OVER w AS lag_35,
        lag(units, {LAG + 14}) OVER w AS lag_42,
        lag(units, {LAG + 21}) OVER w AS lag_49,
        avg(units) OVER (w ROWS BETWEEN {LAG + 6} PRECEDING AND {LAG} PRECEDING)  AS rm7,
        avg(units) OVER (w ROWS BETWEEN {LAG + 27} PRECEDING AND {LAG} PRECEDING) AS rm28,
        avg(units) OVER (w ROWS BETWEEN {LAG + 55} PRECEDING AND {LAG} PRECEDING) AS rm56,
        stddev_samp(units) OVER (w ROWS BETWEEN {LAG + 27} PRECEDING AND {LAG} PRECEDING) AS rsd28,
        avg((units = 0)::FLOAT) OVER (w ROWS BETWEEN {LAG + 27} PRECEDING AND {LAG} PRECEDING) AS zero28,
        price,
        price / avg(price) OVER (w ROWS BETWEEN 55 PRECEDING AND CURRENT ROW) AS price_rel_8w,
        price / lag(price, 7) OVER w AS price_chg_1w,
        isodow(date) AS dow, day(date) AS dom, month(date) AS month, snap::INT AS snap,
        (event_type_1 IS NOT NULL)::INT AS is_event, coalesce(event_type_1, 'none') AS event_type,
        date_diff('day', first_sale, date) AS age_days
    FROM px
    WINDOW w AS (PARTITION BY store, sku ORDER BY date)
    QUALIFY date >= ?
    ORDER BY store, sku, date  -- fixed row order: LightGBM bagging must see identical input every run
    """
    params = [lo, end, price_cutoff, start]
    df = con.execute(q, params).df()
    return _encode(con, df)


def _encode(con: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> pd.DataFrame:
    sku = con.execute("SELECT sku, dept, category FROM core.dim_sku ORDER BY sku").df()
    sku_i = {s: i for i, s in enumerate(sku["sku"])}
    dept_i = {d: i for i, d in enumerate(sorted(sku["dept"].unique()))}
    cat_i = {c: i for i, c in enumerate(sorted(sku["category"].unique()))}
    meta = sku.set_index("sku")
    df["sku_i"] = df["sku"].map(sku_i).astype("int32")
    df["dept_i"] = df["sku"].map(meta["dept"]).map(dept_i).astype("int16")
    df["cat_i"] = df["sku"].map(meta["category"]).map(cat_i).astype("int16")
    df["store_i"] = df["store"].str[-1].astype("int16")
    df["event_i"] = df["event_type"].map({e: i for i, e in enumerate(EVENT_TYPES)}).fillna(0).astype("int16")
    df = df.drop(columns="event_type")
    for c in FEATURES:
        if c not in CATEGORICAL:
            df[c] = df[c].astype(np.float32)
    df["age_days"] = df["age_days"].clip(upper=730)
    return df
