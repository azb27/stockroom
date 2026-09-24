"""The cleaned core layer must recover ground truth for every injected issue.

Run `make data` first. These tests compare warehouse.duckdb (core.*) with
ground_truth.duckdb and dirt_manifest.json, the only place they are ever joined.
"""

from __future__ import annotations

import json

import duckdb
import pytest

from stockroom.config import DIRT_MANIFEST, TRUTH_DB, WAREHOUSE_DB
from stockroom.tools.base import close_all


@pytest.fixture(scope="module")
def con():
    if not WAREHOUSE_DB.exists():
        pytest.skip("run `make data` first")
    close_all()  # DuckDB allows one configuration per file per process; tools use a locked one
    c = duckdb.connect(str(WAREHOUSE_DB), read_only=True)
    c.execute(f"ATTACH '{TRUTH_DB}' AS gt (READ_ONLY)")
    yield c
    c.close()


@pytest.fixture(scope="module")
def manifest():
    m = json.loads(DIRT_MANIFEST.read_text())
    return {i["id"]: i for i in m["issues"]}


def one(con, sql):
    return con.execute(sql).fetchone()[0]


def test_sales_match_truth_except_known_missing_days(con, manifest):
    """D1 + D2 + D3 recovered exactly: every (date, store, sku, units) matches truth."""
    d4 = manifest["D4"]
    missing = ", ".join(f"DATE '{d}'" for d in d4["dates"])
    excl = f"NOT (store = '{d4['store']}' AND date IN ({missing}))"
    diff = one(
        con,
        f"""
        SELECT count(*) FROM (
            (SELECT date, store, sku, units FROM core.fact_sales_daily WHERE {excl}
             EXCEPT ALL
             SELECT date, store_id, item_id, units FROM gt.truth.sales
             WHERE NOT (store_id = '{d4["store"]}' AND date IN ({missing})))
            UNION ALL
            (SELECT date, store_id, item_id, units FROM gt.truth.sales
             WHERE NOT (store_id = '{d4["store"]}' AND date IN ({missing}))
             EXCEPT ALL
             SELECT date, store, sku, units FROM core.fact_sales_daily WHERE {excl})
        )
        """,
    )
    assert diff == 0


def test_no_alias_codes_leak_into_core(con):
    assert (
        one(con, "SELECT count(*) FROM core.fact_sales_daily WHERE sku <> upper(trim(replace(sku,'-','_')))")
        == 0
    )
    assert one(con, "SELECT count(DISTINCT sku) FROM core.dim_sku") == one(
        con, "SELECT count(*) FROM gt.truth.items"
    )


def test_alias_detection_matches_manifest(con, manifest):
    injected = {a["variant"] for a in manifest["D1"]["aliases"]}
    detected = {r[0] for r in con.execute("SELECT raw_code FROM core.sku_alias WHERE is_alias").fetchall()}
    assert detected == injected


def test_duplicate_load_detected(con, manifest):
    d3 = manifest["D3"]
    dropped = one(con, "SELECT string_agg(source_file, ',') FROM core.load_batches WHERE load_rank > 1")
    assert dropped == d3["source_file"]


def test_missing_days_flagged_and_christmas_is_closure(con, manifest):
    d4 = manifest["D4"]
    rows = con.execute(
        "SELECT store, CAST(date AS VARCHAR) FROM core.store_day_status WHERE status = 'missing_data' ORDER BY 2"
    ).fetchall()
    assert rows == [(d4["store"], d) for d in d4["dates"]]
    closures = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT CAST(date AS VARCHAR) FROM core.store_day_status WHERE status = 'closed_all_stores'"
        ).fetchall()
    }
    assert closures == {"2014-12-25", "2015-12-25"}


def test_case_pack_imputation_flags_exactly_the_bad_rows(con, manifest):
    flagged = {r[0] for r in con.execute("SELECT sku FROM core.dim_sku WHERE case_pack_imputed").fetchall()}
    assert flagged == set(manifest["D7"]["skus"])
    assert one(con, "SELECT count(*) FROM core.dim_sku WHERE case_pack IS NULL OR case_pack <= 0") == 0


def test_price_unit_errors_corrected_to_truth(con, manifest):
    rows = manifest["D5"]["rows"]
    assert one(con, "SELECT count(*) FROM core.fact_price_weekly WHERE was_corrected") == len(rows)
    for r in rows:
        got = one(
            con,
            f"""SELECT price FROM core.fact_price_weekly
                WHERE store = '{r["store_code"]}' AND sku = '{r["sku_code"]}' AND wm_yr_wk = {r["wm_yr_wk"]}""",
        )
        assert got == pytest.approx(r["true_price"], abs=0.005)


def test_price_gaps_imputed_where_recoverable(con, manifest):
    """Every imputed week was a week we removed. Tail-of-series removals can't be seen as gaps."""
    removed = {(r["store_code"], r["sku_code"], r["wm_yr_wk"]) for r in manifest["D6"]["rows"]}
    imputed = {
        tuple(r)
        for r in con.execute(
            "SELECT store, sku, wm_yr_wk FROM core.fact_price_weekly WHERE is_imputed"
        ).fetchall()
    }
    assert imputed <= removed, f"{len(imputed - removed)} imputed weeks were not injected gaps"
    assert len(imputed) / len(removed) > 0.95


def test_inventory_matches_truth(con):
    diff = one(
        con,
        """SELECT count(*) FROM core.inventory i
           FULL JOIN gt.truth.inventory t ON t.store_id = i.store AND t.item_id = i.sku
           WHERE i.on_hand_units IS DISTINCT FROM t.on_hand_units
              OR i.on_order_units IS DISTINCT FROM t.on_order_units""",
    )
    assert diff == 0
