"""Build truth.duckdb: the clean M5 subset plus deterministic synthetic ERP attributes.

Everything here is "what really happened". The dirt injector derives the customer's
messy warehouse from it, and tests/evals score the cleaned layer against it.

Real (from M5):      calendar, daily unit sales, weekly sell prices, item hierarchy.
Synthetic (seeded):  case packs, unit costs, suppliers, lead times, item status,
                     and the inventory snapshot. M5 has none of these; a distributor
                     cannot run without them. All are labelled synthetic in the README.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

from stockroom.config import AS_OF_DATE, HORIZON_DAYS, RAW_M5_DIR, SEED, START_DATE, STATE, TRUTH_DB

CASE_PACKS = {"FOODS": [12, 24], "HOUSEHOLD": [6, 12], "HOBBIES": [4, 6, 12]}
LEAD_TIMES = [3, 5, 7, 10, 14]


def build_truth(con: duckdb.DuckDBPyConnection | None = None) -> None:
    TRUTH_DB.parent.mkdir(parents=True, exist_ok=True)
    TRUTH_DB.unlink(missing_ok=True)
    con = con or duckdb.connect(str(TRUTH_DB))
    rng = np.random.default_rng(SEED)
    cal_csv = RAW_M5_DIR / "calendar.csv"
    sales_csv = RAW_M5_DIR / "sales_train_evaluation.csv"
    price_csv = RAW_M5_DIR / "sell_prices.csv"

    con.execute("CREATE SCHEMA truth")
    con.execute(
        f"""
        CREATE TABLE truth.calendar AS
        SELECT CAST(date AS DATE) AS date, d, wm_yr_wk, weekday, month, year,
               event_name_1, event_type_1, event_name_2, event_type_2,
               CAST(snap_{STATE} AS BOOLEAN) AS snap
        FROM read_csv_auto('{cal_csv}')
        WHERE CAST(date AS DATE) BETWEEN DATE '{START_DATE}' AND DATE '{AS_OF_DATE}'
        """
    )
    # The event/SNAP calendar is published in advance, so the next HORIZON_DAYS are known today.
    con.execute(
        f"""
        CREATE TABLE truth.calendar_future AS
        SELECT CAST(date AS DATE) AS date, wm_yr_wk, weekday, month, year,
               event_name_1, event_type_1, event_name_2, event_type_2,
               CAST(snap_{STATE} AS BOOLEAN) AS snap
        FROM read_csv_auto('{cal_csv}')
        WHERE CAST(date AS DATE) > DATE '{AS_OF_DATE}'
          AND CAST(date AS DATE) <= DATE '{AS_OF_DATE}' + INTERVAL {HORIZON_DAYS} DAY
        """
    )
    days = [r[0] for r in con.execute("SELECT d FROM truth.calendar ORDER BY date").fetchall()]
    cols = ", ".join(days)

    # Wide (one column per day) -> long, non-zero rows only. POS systems don't log zeros.
    con.execute(
        f"""
        CREATE TABLE truth.sales AS
        WITH w AS (
            SELECT item_id, store_id, {cols}
            FROM read_csv('{sales_csv}', header = true, auto_detect = true)
            WHERE state_id = '{STATE}'
        ), u AS (
            UNPIVOT w ON {cols} INTO NAME d VALUE units
        )
        SELECT c.date, u.store_id, u.item_id, CAST(u.units AS INTEGER) AS units
        FROM u JOIN truth.calendar c USING (d)
        WHERE u.units > 0
        ORDER BY c.date, u.store_id, u.item_id
        """
    )
    con.execute(
        f"""
        CREATE TABLE truth.items AS
        SELECT DISTINCT item_id, dept_id, cat_id
        FROM read_csv('{sales_csv}', header = true, auto_detect = true)
        WHERE state_id = '{STATE}'
        ORDER BY item_id
        """
    )
    con.execute(
        f"""
        CREATE TABLE truth.prices AS
        SELECT p.store_id, p.item_id, p.wm_yr_wk, round(p.sell_price, 2) AS price
        FROM read_csv_auto('{price_csv}') p
        WHERE p.store_id LIKE '{STATE}_%'
          AND p.wm_yr_wk IN (SELECT DISTINCT wm_yr_wk FROM truth.calendar)
        ORDER BY 1, 2, 3
        """
    )
    con.execute(
        """
        CREATE TABLE truth.stores AS
        SELECT DISTINCT store_id, 'Retail account ' || store_id AS account_name
        FROM truth.sales ORDER BY 1
        """
    )

    _synthetic_attributes(con, rng)
    _inventory_snapshot(con, rng)
    con.close()


def _synthetic_attributes(con: duckdb.DuckDBPyConnection, rng: np.random.Generator) -> None:
    items = con.execute("SELECT * FROM truth.items ORDER BY item_id").df()
    med_price = (
        con.execute("SELECT item_id, median(price) AS med FROM truth.prices GROUP BY 1")
        .df()
        .set_index("item_id")["med"]
    )
    last_sale = (
        con.execute("SELECT item_id, max(date) AS last_sale FROM truth.sales GROUP BY 1")
        .df()
        .set_index("item_id")["last_sale"]
    )

    # 14 suppliers, each serving one department.
    depts = sorted(items["dept_id"].unique())
    sup_rows, dept_sups = [], {}
    n = 1
    for dept in depts:
        k = 3 if dept.startswith("FOODS") else 2
        dept_sups[dept] = []
        for _ in range(k):
            sid = f"S{n:02d}"
            sup_rows.append(
                {
                    "supplier_id": sid,
                    "supplier_name": f"Supplier {sid} ({dept.split('_')[0].title()})",
                    "lead_time_days": int(rng.choice(LEAD_TIMES)),
                    "min_order_cases": int(rng.choice([1, 2, 5, 10])),
                }
            )
            dept_sups[dept].append(sid)
            n += 1
    suppliers = pd.DataFrame(sup_rows)

    as_of = pd.Timestamp(AS_OF_DATE)
    rows = []
    for it in items.itertuples(index=False):
        cat = it.cat_id
        med = float(med_price.get(it.item_id, np.nan))
        ls = last_sale.get(it.item_id)
        stale = ls is None or pd.isna(ls) or (as_of - pd.Timestamp(ls)).days > 56
        dept_num, item_num = it.item_id.split("_")[1], it.item_id.split("_")[2]
        rows.append(
            {
                "item_id": it.item_id,
                "description": f"{cat.title()} dept {dept_num} item {item_num}",
                "dept_id": it.dept_id,
                "cat_id": cat,
                "case_pack": int(rng.choice(CASE_PACKS[cat])),
                "unit_cost": round(med * rng.uniform(0.55, 0.72), 2) if not np.isnan(med) else None,
                "supplier_id": str(rng.choice(dept_sups[it.dept_id])),
                "status": "DISCONTINUED" if stale else "ACTIVE",
            }
        )
    attrs = pd.DataFrame(rows)
    con.register("attrs_df", attrs)
    con.register("sup_df", suppliers)
    con.execute("CREATE TABLE truth.item_attrs AS SELECT * FROM attrs_df")
    con.execute("CREATE TABLE truth.suppliers AS SELECT * FROM sup_df")


def _inventory_snapshot(con: duckdb.DuckDBPyConnection, rng: np.random.Generator) -> None:
    """On-hand/on-order per store-SKU at AS_OF_DATE, shaped by recent demand.

    ~4% stockouts and ~4% heavy overstock are planted so the reorder and anomaly
    tools have real work to do. Recorded in truth so evals can score them.
    """
    demand = con.execute(
        f"""
        SELECT store_id, item_id,
               sum(units) FILTER (WHERE date > DATE '{AS_OF_DATE}' - INTERVAL 28 DAY) / 28.0 AS avg_28d
        FROM truth.sales
        WHERE date > DATE '{AS_OF_DATE}' - INTERVAL 90 DAY
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    ).df()
    demand["avg_28d"] = demand["avg_28d"].fillna(0.0)
    pack = (
        con.execute("SELECT item_id, case_pack FROM truth.item_attrs").df().set_index("item_id")["case_pack"]
    )

    n = len(demand)
    cover = rng.lognormal(mean=np.log(14), sigma=0.5, size=n)
    on_hand = np.round(demand["avg_28d"].to_numpy() * cover).astype(int)
    on_hand = np.where(demand["avg_28d"].to_numpy() == 0, rng.integers(0, 6, size=n), on_hand)

    u = rng.random(n)
    state = np.where(u < 0.04, "stockout", np.where(u < 0.08, "overstock", "normal"))
    on_hand = np.where(state == "stockout", 0, on_hand)
    on_hand = np.where(state == "overstock", np.maximum(on_hand, 1) * 6 + 24, on_hand)

    packs = demand["item_id"].map(pack).to_numpy()
    has_po = (rng.random(n) < 0.3) & (state != "overstock")
    on_order = np.where(has_po, packs * rng.integers(1, 5, size=n), 0)

    demand["on_hand_units"] = on_hand
    demand["on_order_units"] = on_order
    demand["planted_state"] = state
    con.register("inv_df", demand)
    con.execute(
        f"""
        CREATE TABLE truth.inventory AS
        SELECT DATE '{AS_OF_DATE}' AS as_of_date, store_id, item_id,
               CAST(on_hand_units AS INTEGER) AS on_hand_units,
               CAST(on_order_units AS INTEGER) AS on_order_units, planted_state
        FROM inv_df
        """
    )
