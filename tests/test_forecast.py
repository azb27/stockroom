"""Phase 3: forecasting has no look-ahead, reorder math is right, and only humans approve."""

from __future__ import annotations

import datetime as dt
import math
import shutil
import subprocess
import sys
import time

import duckdb
import numpy as np
import pytest

from stockroom import appdb, config
from stockroom.approvals import ApprovalError, decide, list_drafts
from stockroom.forecast.features import FEATURES, build_frame
from stockroom.tools import TOOLS, call
from stockroom.tools.base import warehouse
from stockroom.tools.reorder import order_quantity

needs_data = pytest.mark.skipif(not config.WAREHOUSE_DB.exists(), reason="run `make data` first")
needs_forecast = pytest.mark.skipif(not config.FORECAST_DB.exists(), reason="run `make forecast` first")


# ---- reorder math, checked by hand -------------------------------------------------------------
def test_order_quantity_hand_checked():
    # 10 days at 2/day -> demand 20. P90 - mean = 1.2816 -> daily sd 1 -> sigma = sqrt(10) = 3.1623.
    # z = 1.645 -> safety 5.2019 -> target 25.2019. Minus 8 on hand and 6 on order -> need 11.2019.
    # Case pack 6 -> ceil(1.867) = 2 cases = 12 units.
    c = order_quantity(np.full(10, 2.0), np.full(10, 2.0 + 1.2816), 8, 6, 6, 1.645)
    assert c.demand == pytest.approx(20.0)
    assert c.sigma == pytest.approx(math.sqrt(10), rel=1e-6)
    assert c.safety == pytest.approx(5.2019, abs=1e-3)
    assert c.need == pytest.approx(11.2019, abs=1e-3)
    assert (c.cases, c.units) == (2, 12)


def test_order_quantity_edges():
    flat = np.full(7, 1.0)
    assert order_quantity(flat, flat, on_hand=50, on_order=0, case_pack=6, z=1.645).cases == 0
    assert (
        order_quantity(flat, flat, on_hand=1, on_order=0, case_pack=6, z=1.645).cases == 1
    )  # need exactly 6
    assert (
        order_quantity(flat, flat - 0.5, on_hand=1, on_order=0, case_pack=6, z=1.645).safety == 0
    )  # P90 < mean
    with pytest.raises(ValueError):
        order_quantity(flat, flat, 0, 0, 0, 1.645)


# ---- no look-ahead in features -------------------------------------------------------------------
@needs_data
def test_features_do_not_see_past_the_origin(tmp_path):
    """Delete every sale and price after the origin; features for the next 28 days must not change."""
    origin = dt.date(2016, 4, 24)
    start, end = origin + dt.timedelta(1), origin + dt.timedelta(28)
    series = [
        ("CA_1", "FOODS_3_090"),
        ("CA_3", "HOUSEHOLD_1_032"),
        ("CA_4", "HOBBIES_1_001"),
        ("CA_2", "FOODS_2_019"),
    ]
    full = build_frame(warehouse().cursor(), start, end, origin, series)

    copy = tmp_path / "truncated.duckdb"
    shutil.copy(config.WAREHOUSE_DB, copy)
    con = duckdb.connect(str(copy))
    wk = con.execute("SELECT wm_yr_wk FROM core.dim_date WHERE date = ?", [origin]).fetchone()[0]
    con.execute("DELETE FROM core.fact_sales_daily WHERE date > ?", [origin])
    con.execute("DELETE FROM core.fact_price_weekly WHERE wm_yr_wk > ?", [wk])
    cut = build_frame(con, start, end, origin, series)
    con.close()

    key = ["store", "sku", "date"]
    m = full.merge(cut, on=key, suffixes=("", "_cut"))
    assert len(m) == len(full) == 4 * 28
    for f in FEATURES:
        a, b = m[f].to_numpy(dtype=float), m[f"{f}_cut"].to_numpy(dtype=float)
        assert np.allclose(a, b, equal_nan=True), f"feature {f} changes when future data is removed"


@needs_data
def test_feature_rows_come_back_in_a_fixed_order():
    """LightGBM bagging samples by row position; unordered input made training non-reproducible."""
    f = build_frame(
        warehouse().cursor(),
        dt.date(2016, 5, 1),
        dt.date(2016, 5, 7),
        dt.date(2016, 5, 7),
        [("CA_2", "FOODS_3_090"), ("CA_1", "HOBBIES_1_001"), ("CA_1", "FOODS_1_001")],
    )
    keys = list(zip(f["store"], f["sku"], f["date"], strict=True))
    assert keys == sorted(keys)


# ---- batch forecasts ------------------------------------------------------------------------------
@needs_forecast
def test_forecast_table_is_complete_and_ordered():
    cur = warehouse(config.FORECAST_DB).cursor()
    n_series, n_days, bad = cur.execute(
        """SELECT count(DISTINCT (store, sku)), count(DISTINCT date),
                  count(*) FILTER (WHERE NOT (0 <= p10 AND p10 <= mean AND mean <= p90))
           FROM forecast"""
    ).fetchone()
    want = (
        warehouse()
        .cursor()
        .execute("SELECT count(DISTINCT (store, sku)) FROM core.fact_sales_daily")
        .fetchone()[0]
    )
    assert (n_series, n_days, bad) == (want, 28, 0)
    meta = dict(cur.execute("SELECT key, value FROM model_meta").fetchall())
    assert {"model_version", "trained_through", "best_iterations", "backtest_origin"} <= set(meta)
    assert cur.execute("SELECT count(*) FROM backtest_metrics").fetchone()[0] >= 10


@needs_forecast
def test_backtest_beats_baseline_and_p90_is_near_calibrated():
    cur = warehouse(config.FORECAST_DB).cursor()
    wm, wb, above = cur.execute(
        "SELECT wape_model, wape_baseline, above_p90 FROM backtest_metrics WHERE level = 'store-SKU-day' AND segment = 'all'"
    ).fetchone()
    assert wm < wb
    assert 0.05 <= above <= 0.15  # target 0.10; safety stock depends on it


@needs_forecast
def test_forecast_demand_matches_table_and_aggregates():
    one = call("forecast_demand", {"sku": "foods-3-090", "store": "CA_3", "horizon_days": 7})
    assert one["data"]["sku"] == "FOODS_3_090" and len(one["data"]["daily"]) == 7
    assert any("Interpreted SKU" in c for c in one["caveats"])
    raw = (
        warehouse(config.FORECAST_DB)
        .cursor()
        .execute(
            "SELECT sum(mean) FROM forecast WHERE sku = 'FOODS_3_090' AND store = 'CA_3' AND date <= DATE '2016-05-29'"
        )
        .fetchone()[0]
    )
    assert one["data"]["total_units"]["mean"] == pytest.approx(raw, abs=0.1)
    allst = call("forecast_demand", {"sku": "FOODS_3_090", "horizon_days": 7})
    per_store = sum(
        call("forecast_demand", {"sku": "FOODS_3_090", "store": s, "horizon_days": 7})["data"]["total_units"][
            "mean"
        ]
        for s in ["CA_1", "CA_2", "CA_3", "CA_4"]
    )
    assert allst["data"]["total_units"]["mean"] == pytest.approx(per_store, abs=0.5)


@needs_forecast
def test_forecast_demand_refuses_out_of_range():
    assert "1-28" in call("forecast_demand", {"sku": "FOODS_3_090", "horizon_days": 60})["error"]
    assert "Closest matches" in call("forecast_demand", {"sku": "FOODS_3_9090"})["error"]


# ---- drafts and approvals -------------------------------------------------------------------------
@pytest.fixture
def temp_app(tmp_path, monkeypatch):
    appdb.close_all()
    monkeypatch.setattr(appdb, "APP_PATH", tmp_path / "app.duckdb")
    yield
    appdb.close_all()


def test_no_tool_can_approve_or_send():
    names = " ".join(TOOLS).lower()
    for verb in ("approve", "send", "submit", "place", "decide"):
        assert verb not in names
    for t in TOOLS.values():
        assert "status" not in t.input_schema["properties"]


@needs_forecast
def test_draft_reorder_writes_pending_drafts_with_correct_math(temp_app):
    out = call("draft_reorder", {"store": "CA_2", "dept": "FOODS_3"})
    assert "error" not in out, out
    drafts = list_drafts()
    assert len(drafts) == len(out["data"]["drafts"]) > 0
    assert set(drafts["status"]) == {"PENDING_APPROVAL"}

    # Recompute one line independently from the raw tables.
    line = out["data"]["top_lines"][0]
    w, f = warehouse().cursor(), warehouse(config.FORECAST_DB).cursor()
    on_hand, on_order, pack, lead = w.execute(
        """SELECT i.on_hand_units, i.on_order_units, k.case_pack, s.lead_time_days FROM core.inventory i
           JOIN core.dim_sku k USING (sku) JOIN core.dim_supplier s USING (supplier_id)
           WHERE i.store = 'CA_2' AND i.sku = ?""",
        [line["sku"]],
    ).fetchone()
    fc = f.execute(
        "SELECT mean, p90 FROM forecast WHERE store = 'CA_2' AND sku = ? AND date <= DATE '2016-05-22' + ? ORDER BY date",
        [line["sku"], lead + 7],
    ).df()
    c = order_quantity(fc["mean"].to_numpy(), fc["p90"].to_numpy(), on_hand, on_order, pack, 1.6449)
    assert (line["order_cases"], line["order_units"]) == (c.cases, c.units)
    assert any("PENDING_APPROVAL" in cv for cv in out["caveats"])


@needs_forecast
def test_below_supplier_minimum_is_flagged_not_inflated(temp_app):
    w = warehouse().cursor()
    # one slow SKU, out of stock with nothing on order but still selling, from a supplier with a 5+ case minimum
    sku = w.execute(
        """SELECT i.sku FROM core.inventory i JOIN core.dim_sku k USING (sku) JOIN core.dim_supplier s USING (supplier_id)
           JOIN (SELECT sku, sum(units) AS u FROM core.fact_sales_daily
                 WHERE store = 'CA_1' AND date > DATE '2016-05-22' - 28 GROUP BY sku) d USING (sku)
           WHERE i.store = 'CA_1' AND s.min_order_cases >= 5 AND k.status = 'ACTIVE'
             AND i.on_hand_units = 0 AND i.on_order_units = 0 AND d.u BETWEEN 5 AND 20
           ORDER BY i.sku LIMIT 1"""
    ).fetchone()[0]
    out = call("draft_reorder", {"store": "CA_1", "skus": [sku]})
    d = out["data"]["drafts"][0]
    assert d["below_supplier_minimum"] and d["cases"] < d["supplier_min_cases"]
    assert any("below" in c and "not inflated" in c for c in out["caveats"])


@needs_forecast
def test_only_a_named_human_can_decide(temp_app):
    out = call("draft_reorder", {"store": "CA_4", "dept": "HOBBIES_2"})
    did = out["data"]["drafts"][0]["draft_id"]
    with pytest.raises(ApprovalError):
        decide(did, "approve", by="")
    with pytest.raises(ApprovalError):
        decide(did, "reject", by="buyer")  # rejection needs a reason
    decide(did, "approve", by="buyer", note="ok")
    assert list_drafts("APPROVED")["draft_id"].tolist() == [did]
    with pytest.raises(ApprovalError):
        decide(did, "reject", by="buyer", note="changed my mind")  # already decided


# ---- app.duckdb is never held open (found while building the P6 MCP server) ----------------------------
def _in_subprocess(code: str, path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", f"import duckdb; con = duckdb.connect({str(path)!r}); {code}"],
        capture_output=True,
        text=True,
        timeout=60,
    )


@needs_forecast
def test_a_second_process_can_approve_while_a_tool_process_is_alive(temp_app):
    """A long-lived tool process (MCP server, web API) must not lock out the human approvals CLI."""
    out = call("draft_reorder", {"store": "CA_4", "dept": "HOBBIES_2"})
    did = out["data"]["drafts"][0]["draft_id"]
    r = _in_subprocess(
        f"con.execute(\"UPDATE po_drafts SET status = 'APPROVED' WHERE draft_id = '{did}'\")", appdb.APP_PATH
    )
    assert r.returncode == 0, r.stderr
    assert list_drafts("APPROVED")["draft_id"].tolist() == [did]


def test_app_session_waits_for_a_briefly_locked_file(temp_app):
    with appdb.app_session():
        pass  # create the file and schema
    holder = subprocess.Popen(
        [sys.executable, "-c", (f"import duckdb, time; c = duckdb.connect({str(appdb.APP_PATH)!r}); "
                                "print('locked', flush=True); time.sleep(1.5)")],
        stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    assert holder.stdout.readline().strip() == "locked"
    t0 = time.monotonic()
    with appdb.app_session() as con:
        assert con.execute("SELECT count(*) FROM po_drafts").fetchone() == (0,)
    holder.wait()
    assert time.monotonic() - t0 > 0.5  # it waited for the lock instead of failing
