"""explain_variance: decompose a revenue change between two periods into volume, mix and price.

For a group with SKUs i, quantities q and average realised prices p in periods a and b:

    volume = (Q_b - Q_a) * (R_a / Q_a)          more or fewer units at period-a average price
    mix    = sum_i q_b,i * p_a,i - Q_b * R_a/Q_a  shift toward pricier / cheaper SKUs
    price  = sum_i q_b,i * (p_b,i - p_a,i)        same units, different prices

volume + mix + price = R_b - R_a exactly (tested). A SKU with no sales in a period
takes its average shelf price in that period, so new or lost SKUs still decompose.
"""

from __future__ import annotations

import datetime as dt

from stockroom.tools.base import ToolError, ToolResult, store_day_caveats, warehouse

GROUPS = {"dept": "k.dept", "category": "k.category", "store": "s.store", "sku": "s.sku"}


def _d(x: str, name: str) -> dt.date:
    try:
        return dt.date.fromisoformat(x)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a date like 2016-04-01 (got {x!r})") from None


def explain_variance(
    period_a_start: str,
    period_a_end: str,
    period_b_start: str,
    period_b_end: str,
    by: str = "dept",
    store: str | None = None,
    limit: int = 15,
) -> ToolResult:
    """Why did revenue change between period A and period B? Volume / mix / price by group."""
    if by not in GROUPS:
        raise ToolError(f"by must be one of {tuple(GROUPS)}")
    a0, a1 = _d(period_a_start, "period_a_start"), _d(period_a_end, "period_a_end")
    b0, b1 = _d(period_b_start, "period_b_start"), _d(period_b_end, "period_b_end")
    if a0 > a1 or b0 > b1:
        raise ToolError("each period's start must be on or before its end")
    cur = warehouse().cursor()
    lo, hi = cur.execute("SELECT min(date), max(date) FROM core.dim_date").fetchone()
    for x in (a0, a1, b0, b1):
        if not lo <= x <= hi:
            raise ToolError(f"{x} is outside the data ({lo} to {hi})")
    grp = GROUPS[by]
    store_f = "AND s.store = ?" if store else ""
    sp = [store] if store else []

    df = cur.execute(
        f"""
        WITH per AS (
            SELECT s.store, s.sku, {grp} AS grp,
                   sum(s.units) FILTER (WHERE s.date BETWEEN ? AND ?) AS q_a,
                   sum(s.revenue) FILTER (WHERE s.date BETWEEN ? AND ?) AS r_a,
                   sum(s.units) FILTER (WHERE s.date BETWEEN ? AND ?) AS q_b,
                   sum(s.revenue) FILTER (WHERE s.date BETWEEN ? AND ?) AS r_b
            FROM core.sales_enriched s JOIN core.dim_sku k USING (sku)
            WHERE (s.date BETWEEN ? AND ? OR s.date BETWEEN ? AND ?) {store_f}
            GROUP BY ALL
        ), shelf AS (
            SELECT p.store, p.sku,
                   avg(p.price) FILTER (WHERE d.date BETWEEN ? AND ?) AS shelf_a,
                   avg(p.price) FILTER (WHERE d.date BETWEEN ? AND ?) AS shelf_b
            FROM core.fact_price_weekly p JOIN (SELECT DISTINCT wm_yr_wk, date FROM core.dim_date) d USING (wm_yr_wk)
            GROUP BY ALL
        ), x AS (
            SELECT per.grp, coalesce(q_a, 0) AS q_a, coalesce(r_a, 0) AS r_a,
                   coalesce(q_b, 0) AS q_b, coalesce(r_b, 0) AS r_b,
                   coalesce(r_a / nullif(q_a, 0), shelf_a, r_b / nullif(q_b, 0), shelf_b, 0) AS p_a,
                   coalesce(r_b / nullif(q_b, 0), shelf_b, p_a) AS p_b
            FROM per LEFT JOIN shelf USING (store, sku)
        )
        SELECT grp, sum(q_a) AS units_a, sum(q_b) AS units_b, sum(r_a) AS revenue_a, sum(r_b) AS revenue_b,
               sum(q_b * p_a) AS rev_b_at_a_prices
        FROM x GROUP BY grp
        """,
        [a0, a1, a0, a1, b0, b1, b0, b1, a0, a1, b0, b1, *sp, a0, a1, b0, b1],
    ).df()
    if df.empty:
        raise ToolError("no sales in either period for that selection")

    avg_a = df["revenue_a"] / df["units_a"].where(df["units_a"] > 0)
    df["volume_effect"] = ((df["units_b"] - df["units_a"]) * avg_a).fillna(df["rev_b_at_a_prices"])
    df["mix_effect"] = (df["rev_b_at_a_prices"] - df["units_b"] * avg_a).fillna(0.0)
    df["price_effect"] = df["revenue_b"] - df["rev_b_at_a_prices"]
    df["delta"] = df["revenue_b"] - df["revenue_a"]
    df["pct_change"] = df["delta"] / df["revenue_a"].where(df["revenue_a"] > 0)
    df = df.drop(columns="rev_b_at_a_prices").rename(columns={"grp": by})
    df = df.reindex(df["delta"].abs().sort_values(ascending=False).index)

    num = [
        "units_a",
        "units_b",
        "revenue_a",
        "revenue_b",
        "volume_effect",
        "mix_effect",
        "price_effect",
        "delta",
    ]
    total = {c: float(df[c].sum()) for c in num}
    total["pct_change"] = total["delta"] / total["revenue_a"] if total["revenue_a"] else None

    caveats = []
    la, lb = (a1 - a0).days + 1, (b1 - b0).days + 1
    if la != lb:
        caveats.append(
            f"Periods differ in length ({la} vs {lb} days); the volume effect partly reflects that."
        )
    for s, e, name in ((a0, a1, "Period A"), (b0, b1, "Period B")):
        caveats += [f"{name}: {c}" for c in store_day_caveats(cur, s, e, store)]
    return ToolResult(
        data={
            "by": by,
            "store": store or "all",
            "period_a": [a0, a1],
            "period_b": [b0, b1],
            "total": total,
            "groups": df.head(limit).round(2).to_dict(orient="records"),
            "groups_shown": min(limit, len(df)),
            "groups_total": len(df),
            "method": "volume = change in units at period-A average price; mix = shift between SKUs; "
            "price = change in each SKU's realised price. Components sum to delta.",
        },
        caveats=caveats,
        provenance={"tables": ["core.sales_enriched", "core.fact_price_weekly", "core.dim_sku"]},
    )
