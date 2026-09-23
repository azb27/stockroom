"""Behaviour of the agent-facing tools, checked against ground truth where it exists."""

from __future__ import annotations

import duckdb
import pytest

from stockroom.config import TRUTH_DB, WAREHOUSE_DB
from stockroom.tools import TOOLS, call, sql
from stockroom.tools.anomalies import inventory_anomalies
from stockroom.tools.base import warehouse

pytestmark = pytest.mark.skipif(not WAREHOUSE_DB.exists(), reason="run `make data` first")


@pytest.fixture(scope="module")
def truth():
    c = duckdb.connect(str(TRUTH_DB), read_only=True)
    yield c
    c.close()


# ---- registry ------------------------------------------------------------------------------
def test_every_tool_schema_is_strict_and_documented():
    for t in TOOLS.values():
        assert t.input_schema["additionalProperties"] is False
        assert len(t.description) > 80
        assert set(t.input_schema["required"]) <= set(t.input_schema["properties"])


def test_call_rejects_undeclared_arguments():
    out = call("run_sql", {"query": "SELECT 1", "allowed_schemas": ["raw"]})
    assert "unknown argument" in out["error"]


def test_call_reports_missing_arguments_and_unknown_tools():
    assert "missing required" in call("run_sql", {})["error"]
    assert "unknown tool" in call("send_po_to_supplier", {})["error"]


def test_every_result_has_envelope():
    out = call("run_sql", {"query": "SELECT count(*) AS n FROM core.dim_store"})
    assert set(out) >= {"data", "caveats", "provenance"}
    assert out["data"]["rows"] == [[4]]


# ---- describe_data / run_sql -----------------------------------------------------------------
def test_describe_data_exposes_semantics_and_missing_days():
    out = call("describe_data")
    assert "core.sales_enriched" in out["data"]["tables"]
    assert any("missing_data" in s for s in out["data"]["semantics"])
    assert any("CA_4 2016-03-14" in c and "UNKNOWN" in c for c in out["caveats"])


def test_run_sql_units_match_truth_exactly(truth):
    q = """SELECT sku, sum(units) AS u FROM core.fact_sales_daily
           WHERE store = 'CA_3' AND date BETWEEN '2016-02-01' AND '2016-03-31' AND sku LIKE 'FOODS_3%'
           GROUP BY 1 ORDER BY 2 DESC LIMIT 50"""  # the window where CA_3 logged cases
    got = dict(call("run_sql", {"query": q})["data"]["rows"])
    want = dict(
        truth.execute(
            q.replace("core.fact_sales_daily", "truth.sales")
            .replace("store =", "store_id =")
            .replace("sku", "item_id")
        ).fetchall()
    )
    assert got == want


def test_run_sql_revenue_within_hundredth_of_percent_of_truth(truth):
    got = call(
        "run_sql", {"query": "SELECT sum(revenue) FROM core.sales_enriched WHERE date >= '2016-01-01'"}
    )
    want = truth.execute(
        """SELECT sum(s.units * p.price) FROM truth.sales s JOIN truth.calendar c USING (date)
           JOIN truth.prices p ON p.store_id = s.store_id AND p.item_id = s.item_id AND p.wm_yr_wk = c.wm_yr_wk
           WHERE s.date >= '2016-01-01'
             AND NOT (s.store_id = 'CA_4' AND s.date IN ('2016-03-14', '2016-03-15'))"""  # outage: unknowable
    ).fetchone()[0]
    assert got["data"]["rows"][0][0] == pytest.approx(want, rel=1e-4)


def test_run_sql_truncates_and_says_so():
    out = call("run_sql", {"query": "SELECT * FROM core.dim_sku", "max_rows": 10})
    assert out["data"]["row_count"] == 10 and out["data"]["truncated"]
    assert any("truncated" in c for c in out["caveats"])


def test_run_sql_sales_queries_carry_missing_day_caveat():
    out = call("run_sql", {"query": "SELECT sum(units) FROM core.fact_sales_daily WHERE store = 'CA_4'"})
    assert any("MISSING DATA" in c for c in out["caveats"])


def test_run_sql_rejection_is_a_recoverable_error():
    out = call("run_sql", {"query": "SELECT * FROM raw.stores"})
    assert out["error"].startswith("Query rejected") and "core.*" in out["error"]


def test_run_sql_timeout(monkeypatch):
    monkeypatch.setattr(sql, "TIMEOUT_S", 0.2)
    out = call("run_sql", {"query": "SELECT count(*) FROM core.fact_sales_daily a, core.fact_sales_daily b"})
    assert "exceeded" in out["error"]


# ---- detect_anomalies ------------------------------------------------------------------------
@pytest.fixture(scope="module")
def inv_vs_truth(truth):
    inv = inventory_anomalies(warehouse().cursor())
    t = truth.execute(
        """SELECT i.store_id AS store, i.item_id AS sku, i.planted_state, coalesce(d.u, 0) / 28.0 AS avg_28d
           FROM truth.inventory i JOIN truth.item_attrs a USING (item_id)
           LEFT JOIN (SELECT store_id, item_id, sum(units) u FROM truth.sales
                      WHERE date > DATE '2016-05-22' - 28 GROUP BY ALL) d
             ON d.store_id = i.store_id AND d.item_id = i.item_id
           WHERE a.status = 'ACTIVE'"""
    ).df()
    return t.merge(
        inv[["store", "sku", "anomaly", "on_hand_units", "avg_daily"]], on=["store", "sku"], how="left"
    )


def test_every_planted_stockout_with_demand_is_found(inv_vs_truth):
    m = inv_vs_truth
    planted = m[(m.planted_state == "stockout") & (m.avg_28d > 0)]
    assert len(planted) > 400
    assert (planted.anomaly == "stockout").all()


def test_flagged_stockouts_are_real(inv_vs_truth):
    flagged = inv_vs_truth[inv_vs_truth.anomaly == "stockout"]
    assert ((flagged.on_hand_units == 0) & (flagged.avg_daily > 0)).all()


def test_overstock_flags_are_precise(inv_vs_truth):
    flagged = inv_vs_truth[inv_vs_truth.anomaly.isin(["overstock", "dead_stock"])]
    assert (flagged.planted_state == "overstock").mean() > 0.95


def test_inventory_scope_refuses_historical_dates():
    out = call("detect_anomalies", {"scope": "inventory", "end_date": "2016-03-20"})
    assert "inventory" not in out["data"]
    assert any("snapshot" in c for c in out["caveats"])


def test_anomalies_reject_future_dates():
    assert "after the as-of date" in call("detect_anomalies", {"end_date": "2016-06-01"})["error"]


def test_filters_compose():
    out = call(
        "detect_anomalies", {"scope": "sales", "store": "CA_4", "dept": "FOODS_3", "end_date": "2016-03-20"}
    )
    assert "error" not in out
    assert all(r["store"] == "CA_4" for r in out["data"]["sales"]["top"])
    assert any("CA_4 2016-03-14" in c for c in out["caveats"])


# ---- explain_variance -------------------------------------------------------------------------
@pytest.mark.parametrize("by", ["dept", "category", "store", "sku"])
def test_variance_components_sum_to_delta(by):
    out = call(
        "explain_variance",
        {
            "period_a_start": "2016-03-26",
            "period_a_end": "2016-04-22",
            "period_b_start": "2016-04-23",
            "period_b_end": "2016-05-20",
            "by": by,
            "limit": 50,
        },
    )
    tot = out["data"]["total"]
    assert tot["volume_effect"] + tot["mix_effect"] + tot["price_effect"] == pytest.approx(
        tot["delta"], abs=0.05
    )
    for g in out["data"]["groups"]:
        assert g["volume_effect"] + g["mix_effect"] + g["price_effect"] == pytest.approx(g["delta"], abs=0.05)


def test_variance_totals_match_run_sql():
    out = call(
        "explain_variance",
        {
            "period_a_start": "2016-04-01",
            "period_a_end": "2016-04-14",
            "period_b_start": "2016-04-15",
            "period_b_end": "2016-04-28",
            "store": "CA_2",
        },
    )
    rev_b = call(
        "run_sql",
        {
            "query": "SELECT sum(revenue) FROM core.sales_enriched "
            "WHERE store = 'CA_2' AND date BETWEEN '2016-04-15' AND '2016-04-28'"
        },
    )
    assert out["data"]["total"]["revenue_b"] == pytest.approx(rev_b["data"]["rows"][0][0], abs=0.05)


def test_variance_warns_on_outage_and_unequal_periods():
    out = call(
        "explain_variance",
        {
            "period_a_start": "2016-02-27",
            "period_a_end": "2016-03-25",
            "period_b_start": "2016-03-26",
            "period_b_end": "2016-04-01",
            "store": "CA_4",
        },
    )
    joined = " ".join(out["caveats"])
    assert "differ in length" in joined and "CA_4 2016-03-14" in joined


def test_variance_rejects_bad_input():
    assert (
        "outside the data"
        in call(
            "explain_variance",
            {
                "period_a_start": "2013-01-01",
                "period_a_end": "2013-01-07",
                "period_b_start": "2016-01-01",
                "period_b_end": "2016-01-07",
            },
        )["error"]
    )
    assert (
        "must be a date"
        in call(
            "explain_variance",
            {
                "period_a_start": "last week",
                "period_a_end": "2016-01-07",
                "period_b_start": "2016-01-01",
                "period_b_end": "2016-01-07",
            },
        )["error"]
    )
