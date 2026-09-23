"""Derive the customer's messy warehouse (raw.*) from ground truth, injecting known issues.

Every injected issue is written to dirt_manifest.json with the exact keys it touched.
The manifest is ground truth for tests/evals. The cleaning layer must DETECT these
issues from the raw data alone. It never reads the manifest.

Issue catalogue (each mirrors something seen in real distributor data):
  D1 sku_alias          ERP migration on 2015-11-01 re-coded SKUs (FOODS_3_090 -> FOODS-3-090);
                        POS terminals also send lowercase / trailing-space codes.
  D2 uom_cases          One store logged some SKUs in cases, not eaches, for two months;
                        the uom column is inconsistent ('CS', 'cs', 'Case').
  D3 duplicate_load     One weekly POS file was loaded twice (new batch id, same source file).
  D4 missing_days       One store's POS feed dropped two full days (outage), unlike
                        Christmas, when every store is closed.
  D5 price_unit_error   Some price-book rows were entered in cents (958.00 instead of 9.58).
  D6 price_gap          Some weekly price-book rows are missing.
  D7 case_pack_missing  Some SKU master rows have case_pack NULL or 0.
"""

from __future__ import annotations

import json

import duckdb
import numpy as np
import pandas as pd

from stockroom.config import DIRT_MANIFEST, SEED, TRUTH_DB, WAREHOUSE_DB

MIGRATION_DATE = "2015-11-01"
UOM_STORE, UOM_DEPT, UOM_START, UOM_END = "CA_3", "FOODS_3", "2016-02-01", "2016-03-31"
DUP_STORE, DUP_WEEK = "CA_2", 11611  # the Walmart week containing 2016-04-11
MISSING_STORE, MISSING_DAYS = "CA_4", ["2016-03-14", "2016-03-15"]


def _variant(code: str, kind: str) -> str:
    if kind == "hyphen":
        return code.replace("_", "-")
    if kind == "lower":
        return code.lower()
    if kind == "trailing_space":
        return code + " "
    raise ValueError(kind)


def build_warehouse() -> dict:
    rng = np.random.default_rng(SEED + 1)
    WAREHOUSE_DB.unlink(missing_ok=True)
    con = duckdb.connect(str(WAREHOUSE_DB))
    con.execute(f"ATTACH '{TRUTH_DB}' AS gt (READ_ONLY)")
    con.execute("CREATE SCHEMA raw")
    manifest: dict = {"seed": SEED, "issues": []}

    # ---- choose affected keys up front so issue sets are disjoint -------------------
    recent = (
        con.execute(
            f"SELECT DISTINCT item_id FROM gt.truth.sales WHERE date >= DATE '{MIGRATION_DATE}' ORDER BY 1"
        )
        .df()["item_id"]
        .tolist()
    )
    uom_pool = (
        con.execute(
            f"""SELECT item_id FROM gt.truth.sales s JOIN gt.truth.items i USING (item_id)
            WHERE store_id = '{UOM_STORE}' AND dept_id = '{UOM_DEPT}'
              AND date BETWEEN DATE '{UOM_START}' AND DATE '{UOM_END}'
            GROUP BY 1 HAVING count(*) >= 20 ORDER BY 1"""
        )
        .df()["item_id"]
        .tolist()
    )

    uom_skus = sorted(rng.choice(uom_pool, 15, replace=False).tolist())
    alias_pool = [s for s in recent if s not in set(uom_skus)]
    alias_skus = sorted(rng.choice(alias_pool, 40, replace=False).tolist())
    kinds = ["hyphen"] * 25 + ["lower"] * 10 + ["trailing_space"] * 5
    alias_map = {s: (k, _variant(s, k)) for s, k in zip(alias_skus, rng.permutation(kinds), strict=True)}
    taken = set(uom_skus) | set(alias_skus)
    all_items = con.execute("SELECT item_id FROM gt.truth.items ORDER BY 1").df()["item_id"].tolist()
    cp_skus = sorted(rng.choice([s for s in all_items if s not in taken], 8, replace=False).tolist())

    # ---- master data -------------------------------------------------------------------
    master = con.execute(
        """SELECT item_id AS sku_code, description, dept_id AS dept, cat_id AS category,
                  'EA' AS base_uom, case_pack, unit_cost, supplier_id, status,
                  DATE '2011-01-01' AS created_at
           FROM gt.truth.item_attrs ORDER BY item_id"""
    ).df()
    master["case_pack"] = master["case_pack"].astype("Int64")
    for i, s in enumerate(cp_skus):  # D7
        master.loc[master.sku_code == s, "case_pack"] = pd.NA if i % 2 == 0 else 0
    hyphen_rows = master[master.sku_code.isin([s for s, (k, _) in alias_map.items() if k == "hyphen"])].copy()
    hyphen_rows["sku_code"] = hyphen_rows["sku_code"].map(lambda s: alias_map[s][1])
    hyphen_rows["created_at"] = pd.Timestamp(MIGRATION_DATE).date()
    master = pd.concat([master, hyphen_rows], ignore_index=True)
    con.register("master_df", master)
    con.execute("CREATE TABLE raw.erp_sku_master AS SELECT * FROM master_df")
    con.execute("CREATE TABLE raw.erp_suppliers AS SELECT * FROM gt.truth.suppliers")
    con.execute("CREATE TABLE raw.stores AS SELECT store_id AS store_code, account_name FROM gt.truth.stores")
    con.execute("CREATE TABLE raw.calendar AS SELECT * FROM gt.truth.calendar")

    manifest["issues"].append(
        {
            "id": "D7",
            "type": "case_pack_missing",
            "table": "raw.erp_sku_master",
            "skus": cp_skus,
            "detail": "case_pack set to NULL (even idx) or 0 (odd idx)",
        }
    )

    # ---- POS sales lines + load log ----------------------------------------------------
    con.execute(
        """
        CREATE TEMP TABLE lines AS
        SELECT s.date AS txn_date, s.store_id AS store_code, s.item_id AS sku_code,
               CAST(s.units AS DOUBLE) AS qty, 'EA' AS uom, c.wm_yr_wk,
               'pos_' || s.store_id || '_' || c.wm_yr_wk || '.csv' AS source_file
        FROM gt.truth.sales s JOIN gt.truth.calendar c USING (date)
        """
    )
    # D4: drop two whole days for one store
    n_missing = con.execute(
        f"SELECT count(*) FROM lines WHERE store_code = '{MISSING_STORE}' "
        f"AND txn_date IN ({', '.join(f'DATE {d!r}' for d in MISSING_DAYS)})"
    ).fetchone()[0]
    con.execute(
        f"DELETE FROM lines WHERE store_code = '{MISSING_STORE}' "
        f"AND txn_date IN ({', '.join(f'DATE {d!r}' for d in MISSING_DAYS)})"
    )
    manifest["issues"].append(
        {
            "id": "D4",
            "type": "missing_days",
            "table": "raw.pos_sales_lines",
            "store": MISSING_STORE,
            "dates": MISSING_DAYS,
            "rows_removed": int(n_missing),
        }
    )

    # D2: cases instead of eaches
    case_pack = dict(con.execute("SELECT item_id, case_pack FROM gt.truth.item_attrs").fetchall())
    uom_df = pd.DataFrame({"sku_code": uom_skus, "cp": [case_pack[s] for s in uom_skus]})
    con.register("uom_df", uom_df)
    uom_labels = ["CS", "cs", "Case"]
    con.execute(
        f"""
        UPDATE lines SET qty = round(lines.qty / uom_df.cp, 3),
               uom = CASE hash(lines.txn_date, lines.sku_code) % 3
                         WHEN 0 THEN '{uom_labels[0]}' WHEN 1 THEN '{uom_labels[1]}' ELSE '{uom_labels[2]}' END
        FROM uom_df
        WHERE lines.sku_code = uom_df.sku_code AND lines.store_code = '{UOM_STORE}'
          AND lines.txn_date BETWEEN DATE '{UOM_START}' AND DATE '{UOM_END}'
        """
    )
    n_uom = con.execute("SELECT count(*) FROM lines WHERE uom <> 'EA'").fetchone()[0]
    manifest["issues"].append(
        {
            "id": "D2",
            "type": "uom_cases",
            "table": "raw.pos_sales_lines",
            "store": UOM_STORE,
            "window": [UOM_START, UOM_END],
            "skus": uom_skus,
            "rows_affected": int(n_uom),
            "uom_labels": uom_labels,
        }
    )

    # D1: alias codes after migration
    amap = pd.DataFrame([{"sku": s, "kind": k, "variant": v} for s, (k, v) in alias_map.items()])
    con.register("amap", amap)
    con.execute(
        f"""UPDATE lines SET sku_code = amap.variant FROM amap
            WHERE lines.sku_code = amap.sku AND lines.txn_date >= DATE '{MIGRATION_DATE}'"""
    )
    n_alias = con.execute("SELECT count(*) FROM lines l JOIN amap ON l.sku_code = amap.variant").fetchone()[0]
    manifest["issues"].append(
        {
            "id": "D1",
            "type": "sku_alias",
            "tables": ["raw.pos_sales_lines", "raw.erp_sku_master", "raw.inventory_snapshot"],
            "migration_date": MIGRATION_DATE,
            "aliases": amap.to_dict(orient="records"),
            "sales_rows_affected": int(n_alias),
            "detail": "hyphen variants also exist as rows in erp_sku_master and in inventory",
        }
    )

    # batches: one per source file, loaded the Saturday after the week closes
    con.execute(
        """
        CREATE TABLE raw.load_log AS
        SELECT row_number() OVER (ORDER BY min(txn_date), source_file) AS load_batch_id,
               source_file, CAST(max(txn_date) + INTERVAL 1 DAY + INTERVAL 2 HOUR AS TIMESTAMP) AS loaded_at,
               count(*) AS row_count
        FROM lines GROUP BY source_file
        """
    )
    con.execute(
        """
        CREATE TABLE raw.pos_sales_lines AS
        SELECT l.txn_date, l.store_code, l.sku_code, l.qty, l.uom, b.load_batch_id
        FROM lines l JOIN raw.load_log b USING (source_file)
        """
    )
    # D3: the same weekly file loaded a second time
    dup_file = f"pos_{DUP_STORE}_{DUP_WEEK}.csv"
    new_id = con.execute("SELECT max(load_batch_id) + 1 FROM raw.load_log").fetchone()[0]
    con.execute(
        f"""INSERT INTO raw.load_log
            SELECT {new_id}, source_file, loaded_at + INTERVAL 3 DAY + INTERVAL 7 HOUR, row_count
            FROM raw.load_log WHERE source_file = '{dup_file}'"""
    )
    con.execute(
        f"""INSERT INTO raw.pos_sales_lines
            SELECT l.txn_date, l.store_code, l.sku_code, l.qty, l.uom, {new_id}
            FROM raw.pos_sales_lines l JOIN raw.load_log b USING (load_batch_id)
            WHERE b.source_file = '{dup_file}' AND l.load_batch_id <> {new_id}"""
    )
    n_dup = con.execute(
        f"SELECT count(*) FROM raw.pos_sales_lines WHERE load_batch_id = {new_id}"
    ).fetchone()[0]
    manifest["issues"].append(
        {
            "id": "D3",
            "type": "duplicate_load",
            "table": "raw.pos_sales_lines",
            "source_file": dup_file,
            "duplicate_batch_id": int(new_id),
            "rows_duplicated": int(n_dup),
        }
    )

    # ---- price book ------------------------------------------------------------------
    prices = con.execute(
        "SELECT store_id AS store_code, item_id AS sku_code, wm_yr_wk, price FROM gt.truth.prices ORDER BY 1, 2, 3"
    ).df()
    grp = prices.groupby(["store_code", "sku_code"])
    first_week = grp["wm_yr_wk"].transform("min") == prices["wm_yr_wk"]
    series_len = grp["wm_yr_wk"].transform("count")

    cents_idx = rng.choice(prices.index[(series_len >= 20) & ~first_week], 25, replace=False)  # D5
    remaining = prices.index[~first_week & ~prices.index.isin(cents_idx)]
    gap_idx = rng.choice(remaining, int(0.015 * len(prices)), replace=False)  # D6

    cents_rows = prices.loc[cents_idx].copy()
    prices.loc[cents_idx, "price"] = (prices.loc[cents_idx, "price"] * 100).round(2)
    gap_rows = prices.loc[gap_idx].copy()
    prices = prices.drop(index=gap_idx)
    # alias codes flow into the price book after migration too (hyphen variants only)
    hy = {s: v for s, (k, v) in alias_map.items() if k == "hyphen"}
    mig_wk = con.execute(
        f"SELECT wm_yr_wk FROM gt.truth.calendar WHERE date = DATE '{MIGRATION_DATE}'"
    ).fetchone()[0]
    mask = prices.sku_code.isin(hy) & (prices.wm_yr_wk >= mig_wk)
    prices.loc[mask, "sku_code"] = prices.loc[mask, "sku_code"].map(hy)
    con.register("prices_df", prices)
    con.execute("CREATE TABLE raw.price_book AS SELECT * FROM prices_df")

    manifest["issues"].append(
        {
            "id": "D5",
            "type": "price_unit_error",
            "table": "raw.price_book",
            "rows": cents_rows.rename(columns={"price": "true_price"}).to_dict(orient="records"),
        }
    )
    manifest["issues"].append(
        {
            "id": "D6",
            "type": "price_gap",
            "table": "raw.price_book",
            "rows_removed": len(gap_rows),
            "rows": gap_rows.rename(columns={"price": "true_price"}).to_dict(orient="records"),
        }
    )

    # ---- inventory snapshot (hyphen aliases, since it post-dates migration) ------------
    con.execute(
        """
        CREATE TABLE raw.inventory_snapshot AS
        SELECT i.as_of_date, i.store_id AS store_code,
               coalesce(a.variant, i.item_id) AS sku_code,
               i.on_hand_units AS on_hand, i.on_order_units AS on_order, 'EA' AS uom
        FROM gt.truth.inventory i
        LEFT JOIN (SELECT * FROM amap WHERE kind = 'hyphen') a ON a.sku = i.item_id
        """
    )

    con.execute("DETACH gt")
    con.close()
    DIRT_MANIFEST.write_text(json.dumps(manifest, indent=2, default=str))
    return manifest
