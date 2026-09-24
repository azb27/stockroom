"""Build the 120-question eval set. Deterministic: same data + seed -> identical file.

    python -m evals.build_questions      # writes evals/questions.jsonl

Four tiers of 30. Parameters (stores, SKUs, dates) are sampled with a fixed seed from the data;
expected answers are computed from ground_truth.duckdb (ADR 0003), except forecast and reorder
questions, whose "truth" is what the model/policy outputs (forecast.duckdb + the reorder formula),
because those questions test tool use, not forecast accuracy (the backtest covers that).

Many questions are deliberately placed on the injected data problems (re-coded SKUs, the
cases-not-units window, the double-loaded week, the outage, cent-keyed prices), because that is
where a naive agent over raw data goes wrong.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import math
import re
from pathlib import Path
from statistics import NormalDist

import duckdb
import numpy as np
import pandas as pd

from stockroom import config

OUT = Path(__file__).parent / "questions.jsonl"
AS_OF = dt.date.fromisoformat(config.AS_OF_DATE)
SEED = 2027
MISSING = ("CA_4", [dt.date(2016, 3, 14), dt.date(2016, 3, 15)])
DUP_WEEK = (dt.date(2016, 4, 9), dt.date(2016, 4, 15))  # Walmart week 11611, loaded twice for CA_2
Z80 = 1.2816


def overlaps_outage(store: str | None, d0: dt.date, d1: dt.date) -> bool:
    return store in (None, MISSING[0]) and any(d0 <= d <= d1 for d in MISSING[1])


def fmt_date(d: dt.date, style: int) -> str:
    return d.isoformat() if style % 2 == 0 else f"{calendar.month_name[d.month]} {d.day}, {d.year}"


class Builder:
    def __init__(self) -> None:
        self.t = duckdb.connect(str(config.TRUTH_DB), read_only=True)
        self.f = duckdb.connect(str(config.FORECAST_DB), read_only=True)
        self.rng = np.random.default_rng(SEED)
        self.manifest = {i["id"]: i for i in json.loads(config.DIRT_MANIFEST.read_text())["issues"]}
        self.qs: list[dict] = []

    # ---- helpers ----------------------------------------------------------------------------------
    def add(
        self, tier: str, kind: str, question: str, expected, scorer: str, tol: float = 0.0, **meta
    ) -> None:
        n = sum(1 for q in self.qs if q["tier"] == tier) + 1
        self.qs.append(
            {"id": f"{tier}-{n:02d}", "tier": tier, "kind": kind, "question": question,
             "expected": expected, "scorer": scorer, "tol": tol, **meta}
        )  # fmt: skip

    def one(self, sql: str, params: list | None = None):
        return self.t.execute(sql, params or []).fetchone()[0]

    def pick(self, df: pd.DataFrame, n: int) -> pd.DataFrame:
        idx = self.rng.choice(len(df), size=n, replace=False)
        return df.iloc[sorted(idx)]

    def units(self, store: str | None, sku: str | None, d0: dt.date, d1: dt.date, where: str = "") -> int:
        cond, p = ["date BETWEEN ? AND ?"], [d0, d1]
        if store:
            cond.append("store_id = ?")
            p.append(store)
        if sku:
            cond.append("item_id = ?")
            p.append(sku)
        return int(self.one(
            f"SELECT coalesce(sum(units), 0) FROM truth.sales s JOIN truth.items i USING (item_id) "
            f"WHERE {' AND '.join(cond)} {where}", p))  # fmt: skip

    def revenue(self, where: str, p: list) -> float:
        return float(self.one(
            f"""SELECT coalesce(sum(s.units * pr.price), 0) FROM truth.sales s JOIN truth.calendar c USING (date)
                JOIN truth.items i USING (item_id)
                JOIN truth.prices pr ON pr.store_id = s.store_id AND pr.item_id = s.item_id AND pr.wm_yr_wk = c.wm_yr_wk
                WHERE {where}""", p))  # fmt: skip

    # ---- tier 1: lookups ----------------------------------------------------------------------------
    def tier1(self) -> None:
        alias = {a["sku"] for a in self.manifest["D1"]["aliases"]}
        d2 = self.manifest["D2"]
        base = self.t.execute(
            """SELECT date, store_id, item_id, units FROM truth.sales
               WHERE date BETWEEN '2016-01-04' AND '2016-05-22' AND NOT (store_id = 'CA_3' AND date BETWEEN '2016-02-01' AND '2016-03-31')
                 AND NOT (store_id = 'CA_4' AND date IN ('2016-03-14', '2016-03-15'))
               ORDER BY date, store_id, item_id"""
        ).df()
        base = base[~base.item_id.isin(alias)]
        for i, r in enumerate(self.pick(base[base.units >= 2], 3).itertuples()):
            self.add("T1", "units_on_day", f"How many units of {r.item_id} did {r.store_id} sell on {fmt_date(r.date.date(), i)}?",
                     int(r.units), "int_exact")  # fmt: skip
        # zero-sale days on open days: a missing row means zero (except outage days)
        z = self.t.execute(
            """WITH s AS (SELECT DISTINCT store_id, item_id FROM truth.sales
                          WHERE date BETWEEN '2016-04-01' AND '2016-04-30')
               SELECT s.store_id, s.item_id, c.date FROM s CROSS JOIN (SELECT date FROM truth.calendar
                   WHERE date BETWEEN '2016-04-04' AND '2016-04-27') c
               ANTI JOIN truth.sales t ON t.store_id = s.store_id AND t.item_id = s.item_id AND t.date = c.date
               ORDER BY s.store_id, s.item_id, c.date"""
        ).df()
        z = z[~z.item_id.isin(alias)]
        for i, r in enumerate(self.pick(z, 3).itertuples()):
            self.add("T1", "units_on_day_zero", f"What were {r.store_id}'s unit sales of {r.item_id} on {fmt_date(r.date.date(), i + 1)}?",
                     0, "int_exact", note="no sales row on an open day = 0")  # fmt: skip
        cases = self.t.execute(
            "SELECT date, store_id, item_id, units FROM truth.sales WHERE store_id = ? AND item_id IN (SELECT unnest(?)) "
            "AND date BETWEEN ? AND ? AND units >= 3 ORDER BY 1, 3",
            [d2["store"], d2["skus"], *d2["window"]],
        ).df()
        for i, r in enumerate(self.pick(cases, 3).itertuples()):
            self.add("T1", "units_cases_window", f"How many units of {r.item_id} did {r.store_id} sell on {fmt_date(r.date.date(), i)}?",
                     int(r.units), "int_exact", note="raw data logged this in cases")  # fmt: skip
        post = self.t.execute(
            "SELECT date, store_id, item_id, units FROM truth.sales WHERE item_id IN (SELECT unnest(?)) "
            "AND date >= '2015-11-01' AND units >= 2 ORDER BY 1, 2, 3",
            [sorted(alias)],
        ).df()
        for i, r in enumerate(self.pick(post, 3).itertuples()):
            self.add("T1", "units_alias_sku", f"How many units of {r.item_id} did {r.store_id} sell on {fmt_date(r.date.date(), i + 1)}?",
                     int(r.units), "int_exact", note="SKU was re-coded in the ERP migration")  # fmt: skip

        # prices: 4 regular + 2 weeks keyed in cents in the raw price book
        pr = self.t.execute(
            """SELECT p.store_id, p.item_id, p.wm_yr_wk, p.price, min(c.date) AS d FROM truth.prices p
               JOIN truth.calendar c USING (wm_yr_wk) WHERE p.wm_yr_wk BETWEEN 11601 AND 11616 GROUP BY ALL ORDER BY 1, 2, 3"""
        ).df()
        for i, r in enumerate(self.pick(pr, 4).itertuples()):
            d = r.d.date() + dt.timedelta(days=int(self.rng.integers(0, 7)))
            self.add("T1", "price_in_week", f"What was the shelf price of {r.item_id} at {r.store_id} in the week containing {fmt_date(d, i)}?",
                     float(r.price), "num", tol=0.005)  # fmt: skip
        cents = pd.DataFrame(self.manifest["D5"]["rows"]).sort_values(["store_code", "sku_code", "wm_yr_wk"])
        for i, r in enumerate(self.pick(cents, 2).itertuples()):
            d = self.one(
                "SELECT min(date) FROM truth.calendar WHERE wm_yr_wk = ?", [int(r.wm_yr_wk)]
            ) + dt.timedelta(days=2)
            self.add("T1", "price_cents_error", f"What was the shelf price of {r.sku_code} at {r.store_code} in the week containing {fmt_date(d, i)}?",
                     float(r.true_price), "num", tol=0.005, note="raw price book has this week keyed in cents")  # fmt: skip

        inv = self.t.execute(
            "SELECT i.store_id, i.item_id, i.on_hand_units FROM truth.inventory i JOIN truth.item_attrs a USING (item_id) "
            "WHERE a.status = 'ACTIVE' AND i.on_hand_units > 0 ORDER BY 1, 2"
        ).df()
        hy = [a["sku"] for a in self.manifest["D1"]["aliases"] if a["kind"] == "hyphen"]
        for i, r in enumerate(self.pick(inv[~inv.item_id.isin(alias)], 4).itertuples()):
            self.add("T1", "on_hand", f"How many units of {r.item_id} does {r.store_id} have on hand today?" if i % 2 == 0 else
                     f"What is {r.store_id}'s current on-hand stock of {r.item_id}?", int(r.on_hand_units), "int_exact")  # fmt: skip
        for r in self.pick(inv[inv.item_id.isin(hy)], 2).itertuples():
            self.add("T1", "on_hand_alias", f"How many units of {r.item_id} does {r.store_id} have on hand today?",
                     int(r.on_hand_units), "int_exact", note="inventory system uses the post-migration code")  # fmt: skip

        lt = self.t.execute(
            "SELECT a.item_id, s.lead_time_days FROM truth.item_attrs a JOIN truth.suppliers s USING (supplier_id) ORDER BY 1"
        ).df()
        for r in self.pick(lt, 3).itertuples():
            self.add(
                "T1",
                "lead_time",
                f"What is the supplier lead time, in days, for {r.item_id}?",
                int(r.lead_time_days),
                "int_exact",
            )

        dup = self.t.execute(
            "SELECT item_id, sum(units) u FROM truth.sales WHERE store_id = 'CA_2' AND date BETWEEN ? AND ? "
            "GROUP BY 1 HAVING sum(units) >= 10 ORDER BY 1",
            list(DUP_WEEK),
        ).df()
        for i, r in enumerate(self.pick(dup, 2).itertuples()):
            self.add("T1", "week_units_dup_load", f"How many units of {r.item_id} did CA_2 sell in total from {fmt_date(DUP_WEEK[0], i)} "
                     f"to {fmt_date(DUP_WEEK[1], i)}?", int(r.u), "int_exact", note="this week's POS file was loaded twice")  # fmt: skip
        wk = self.t.execute(
            "SELECT store_id, item_id, sum(units) u FROM truth.sales WHERE date BETWEEN '2016-05-09' AND '2016-05-15' "
            "AND store_id <> 'CA_2' GROUP BY ALL HAVING sum(units) >= 10 ORDER BY 1, 2"
        ).df()
        r = self.pick(wk[~wk.item_id.isin(alias)], 1).iloc[0]
        self.add(
            "T1",
            "week_units",
            f"How many units of {r.item_id} did {r.store_id} sell from 2016-05-09 to 2016-05-15?",
            int(r.u),
            "int_exact",
        )

    # ---- tier 2: aggregation --------------------------------------------------------------------------
    def tier2(self) -> None:
        combos = [("CA_2", "FOODS_3", 4), ("CA_3", "FOODS_3", 2), ("CA_1", "HOUSEHOLD_1", 3),
                  ("CA_4", "HOBBIES_1", 1), ("CA_2", "HOUSEHOLD_2", 4), ("CA_3", "FOODS_2", 5)]  # fmt: skip
        for store, dept, m in combos:
            d0, d1 = dt.date(2016, m, 1), dt.date(2016, m, calendar.monthrange(2016, m)[1])
            if m == 5:
                d1 = AS_OF
            v = self.revenue(
                "s.store_id = ? AND i.dept_id = ? AND s.date BETWEEN ? AND ?", [store, dept, d0, d1]
            )
            span = f"{calendar.month_name[m]} 2016" + (" (through May 22)" if m == 5 else "")
            self.add("T2", "dept_revenue_month", f"What was total revenue for the {dept} department at {store} in {span}?",
                     round(v, 2), "num", tol=0.01)  # fmt: skip
        for cat, d0, d1 in [("FOODS", "2016-04-11", "2016-04-17"), ("HOUSEHOLD", "2016-02-01", "2016-02-29"),
                            ("HOBBIES", "2016-05-01", "2016-05-21"), ("FOODS", "2016-01-10", "2016-01-23")]:  # fmt: skip
            v = self.units(
                None, None, dt.date.fromisoformat(d0), dt.date.fromisoformat(d1), f"AND i.cat_id = '{cat}'"
            )
            self.add(
                "T2",
                "category_units_range",
                f"How many {cat} units were sold across all four stores from {d0} to {d1}?",
                v,
                "int_exact",
            )
        n = 0
        for store, dept, m in [("CA_1", "FOODS_3", 3), ("CA_2", "HOUSEHOLD_1", 4), ("CA_3", "HOBBIES_1", 1),
                               ("CA_4", "FOODS_2", 2), ("CA_1", "HOUSEHOLD_2", 5), ("CA_3", "FOODS_1", 4),
                               ("CA_2", "FOODS_3", 2), ("CA_4", "HOUSEHOLD_1", 3)]:  # fmt: skip
            if n == 6:
                break
            d0, d1 = dt.date(2016, m, 1), min(AS_OF, dt.date(2016, m, calendar.monthrange(2016, m)[1]))
            if overlaps_outage(store, d0, d1):  # a top-5 could hinge on unknowable days
                continue
            top = self.t.execute(
                "SELECT item_id, sum(units) u FROM truth.sales s JOIN truth.items i USING (item_id) "
                "WHERE store_id = ? AND dept_id = ? AND date BETWEEN ? AND ? GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 6",
                [store, dept, d0, d1],
            ).fetchall()
            if top[4][1] == top[5][1]:  # tie at the cut-off: ambiguous, skip
                continue
            n += 1
            self.add("T2", "top5_skus", f"What were the top 5 {dept} SKUs by units sold at {store} in {calendar.month_name[m]} 2016"
                     f"{' (through May 22)' if m == 5 else ''}?", sorted(r[0] for r in top[:5]), "set_exact")  # fmt: skip
        for cat, m in [("HOUSEHOLD", 3), ("HOBBIES", 4), ("FOODS", 1), ("HOBBIES", 2)]:
            d0, d1 = dt.date(2016, m, 1), dt.date(2016, m, calendar.monthrange(2016, m)[1])
            rows = self.t.execute(
                """SELECT s.store_id, sum(s.units * pr.price) r FROM truth.sales s JOIN truth.calendar c USING (date)
                   JOIN truth.items i USING (item_id) JOIN truth.prices pr ON pr.store_id = s.store_id AND pr.item_id = s.item_id
                   AND pr.wm_yr_wk = c.wm_yr_wk WHERE i.cat_id = ? AND s.date BETWEEN ? AND ? GROUP BY 1 ORDER BY 2 DESC""",
                [cat, d0, d1],
            ).fetchall()
            self.add("T2", "top_store", f"Which store had the highest {cat} revenue in {calendar.month_name[m]} 2016?",
                     rows[0][0], "label", margin=round((rows[0][1] - rows[1][1]) / rows[1][1], 4))  # fmt: skip
        for store, dept, m in [
            ("CA_1", "HOBBIES_2", 4),
            ("CA_4", "FOODS_1", 3),
            ("CA_3", "HOUSEHOLD_2", 2),
            ("CA_2", "FOODS_2", 1),
        ]:
            d0, d1 = dt.date(2016, m, 1), dt.date(2016, m, calendar.monthrange(2016, m)[1])
            q = f"How many different {dept} SKUs sold at least one unit at {store} in {calendar.month_name[m]} 2016?"
            if overlaps_outage(store, d0, d1):
                # The true count is unknowable from any data the agent has; the right answer is the known-days
                # count *with the outage flagged*. (Found in the first run: T2-22 originally expected 192.)
                v = self.one("SELECT count(DISTINCT item_id) FROM truth.sales s JOIN truth.items i USING (item_id) "
                             "WHERE store_id = ? AND dept_id = ? AND date BETWEEN ? AND ? "
                             "AND date NOT IN ('2016-03-14', '2016-03-15')", [store, dept, d0, d1])  # fmt: skip
                self.add("T2", "distinct_skus_sold", q, int(v), "unknown_or_flagged_partial", tol=0.0,
                         note="window covers the CA_4 outage: known-days count, outage must be flagged")  # fmt: skip
            else:
                v = self.one("SELECT count(DISTINCT item_id) FROM truth.sales s JOIN truth.items i USING (item_id) "
                             "WHERE store_id = ? AND dept_id = ? AND date BETWEEN ? AND ?", [store, dept, d0, d1])  # fmt: skip
                self.add("T2", "distinct_skus_sold", q, int(v), "int_exact")
        for store, m in [("CA_1", 4), ("CA_3", 1), ("CA_4", 2)]:
            d0, d1 = dt.date(2016, m, 1), dt.date(2016, m, calendar.monthrange(2016, m)[1])
            foods = self.revenue(
                "s.store_id = ? AND i.cat_id = 'FOODS' AND s.date BETWEEN ? AND ?", [store, d0, d1]
            )
            total = self.revenue("s.store_id = ? AND s.date BETWEEN ? AND ?", [store, d0, d1])
            self.add("T2", "category_share_pct", f"What percentage of {store}'s total revenue in {calendar.month_name[m]} 2016 came from FOODS?",
                     round(100 * foods / total, 2), "num_abs", tol=0.2, unit="percent")  # fmt: skip
        for store, end in [
            ("CA_2", dt.date(2016, 5, 22)),
            ("CA_1", dt.date(2016, 5, 1)),
            ("CA_3", dt.date(2016, 4, 24)),
        ]:
            cur = self.units(store, None, end - dt.timedelta(days=6), end)
            prev = self.units(store, None, end - dt.timedelta(days=13), end - dt.timedelta(days=7))
            self.add("T2", "wow_change_pct", f"By what percentage did total unit sales at {store} change in the 7 days ending "
                     f"{end} compared with the 7 days before that?", round(100 * (cur - prev) / prev, 2), "num_abs", tol=0.2, unit="percent")  # fmt: skip

    # ---- tier 3: multi-step -----------------------------------------------------------------------------
    def _inv_frame(self) -> pd.DataFrame:
        return self.t.execute(
            """SELECT i.store_id, i.item_id, a.dept_id, a.case_pack, a.status, s.lead_time_days,
                      i.on_hand_units, i.on_order_units, coalesce(d.u, 0) / 28.0 AS avg_daily
               FROM truth.inventory i JOIN truth.item_attrs a USING (item_id) JOIN truth.suppliers s USING (supplier_id)
               LEFT JOIN (SELECT store_id, item_id, sum(units) u FROM truth.sales WHERE date > DATE '2016-05-22' - 28
                          GROUP BY ALL) d USING (store_id, item_id)
               ORDER BY i.store_id, i.item_id"""
        ).df()

    def tier3(self) -> None:
        inv = self._inv_frame()
        act = inv[inv.status == "ACTIVE"]
        for store, dept in [("CA_1", "FOODS_3"), ("CA_3", "HOUSEHOLD_1"), ("CA_4", "FOODS_2"),
                            ("CA_2", "HOBBIES_1"), ("CA_3", "FOODS_3"), ("CA_1", "HOUSEHOLD_2")]:  # fmt: skip
            v = int(
                (
                    (act.store_id == store)
                    & (act.dept_id == dept)
                    & (act.on_hand_units == 0)
                    & (act.avg_daily > 0)
                ).sum()
            )
            self.add("T3", "stockouts_selling", f"How many active {dept} SKUs at {store} are out of stock right now but "
                     "sold at least one unit in the last 28 days?", v, "int_exact")  # fmt: skip
        for store, dept in [
            ("CA_2", "FOODS_3"),
            ("CA_4", "HOUSEHOLD_1"),
            ("CA_1", "FOODS_2"),
            ("CA_3", "HOBBIES_1"),
            ("CA_2", "HOUSEHOLD_2"),
        ]:
            g = act[
                (act.store_id == store)
                & (act.dept_id == dept)
                & (act.on_hand_units > 0)
                & (act.avg_daily > 0)
            ]
            v = int(((g.on_hand_units + g.on_order_units) / g.avg_daily < g.lead_time_days).sum())
            self.add("T3", "stockout_risk", f"How many active {dept} SKUs at {store} have units on hand right now (on-hand "
                     "above zero) but will run out before a new order could arrive? Compute cover as (on-hand + on-order) "
                     "divided by each SKU's average daily sales over the last 28 days, and compare it with its supplier's "
                     "lead time.", v, "int_exact")  # fmt: skip
        self._variance_questions()
        fc = self.f.execute("SELECT store, sku, date, mean, p90 FROM forecast ORDER BY 1, 2, 3").df()
        tot = (
            fc[fc.date <= pd.Timestamp(AS_OF + dt.timedelta(days=14))]
            .groupby(["store", "sku"])["mean"]
            .sum()
            .reset_index()
        )
        big = tot[tot["mean"] >= 20]
        for i, r in enumerate(self.pick(big, 4).itertuples()):
            h = 7 if i < 2 else 14
            v = fc[
                (fc.store == r.store)
                & (fc.sku == r.sku)
                & (fc.date <= pd.Timestamp(AS_OF + dt.timedelta(days=h)))
            ]["mean"].sum()
            self.add("T3", "forecast_total", f"What is the forecast total demand for {r.sku} at {r.store} over the next {h} days?",
                     round(float(v), 1), "num", tol=0.01, truth_source="forecast.duckdb (model output)")  # fmt: skip
        self._reorder_questions(inv, fc)
        self.add("T3", "dq_missing_days", "Which store-days in the data have missing sales data?",
                 ["CA_4", "2016-03-14", "2016-03-15"], "contains_all")  # fmt: skip
        self.add("T3", "dq_duplicate_file", "Which weekly POS file was loaded into the warehouse twice?",
                 [self.manifest["D3"]["source_file"]], "contains_all")  # fmt: skip
        self.add("T3", "dq_alias_count", "How many non-canonical SKU codes (migration re-codes, lowercase or stray "
                 "whitespace variants) did the data-quality pass find?", len(self.manifest["D1"]["aliases"]), "int_exact")  # fmt: skip
        for dept in ("FOODS_3", "HOUSEHOLD_1"):
            v = self.units("CA_2", None, *DUP_WEEK, f"AND i.dept_id = '{dept}'")
            self.add("T3", "dq_dup_week_units", f"How many {dept} units did CA_2 sell in total from {DUP_WEEK[0]} to {DUP_WEEK[1]}?",
                     v, "int_exact", note="this week's POS file was loaded twice")  # fmt: skip

    def _variance_questions(self) -> None:
        """Five clear-cut 'which group moved most, and why' questions, preferring non-volume answers."""
        pairs = [
            (dt.date(2016, 3, 26), dt.date(2016, 4, 22), dt.date(2016, 4, 23), dt.date(2016, 5, 20)),
            (dt.date(2016, 2, 27), dt.date(2016, 3, 25), dt.date(2016, 3, 26), dt.date(2016, 4, 22)),
            (dt.date(2016, 1, 30), dt.date(2016, 2, 26), dt.date(2016, 2, 27), dt.date(2016, 3, 25)),
        ]
        df = self.t.execute(
            """SELECT s.store_id, i.dept_id, i.cat_id, s.item_id, s.date, s.units, s.units * pr.price AS rev
               FROM truth.sales s JOIN truth.calendar c USING (date) JOIN truth.items i USING (item_id)
               JOIN truth.prices pr ON pr.store_id = s.store_id AND pr.item_id = s.item_id AND pr.wm_yr_wk = c.wm_yr_wk
               WHERE s.date BETWEEN '2016-01-30' AND '2016-05-20'"""
        ).df()
        df["date"] = pd.to_datetime(df["date"])
        outage = (df.store_id == MISSING[0]) & df.date.isin([pd.Timestamp(d) for d in MISSING[1]])

        def decompose(g: pd.DataFrame, by: str) -> pd.DataFrame:
            per = (
                g.groupby([by, "store_id", "item_id", "period"])[["units", "rev"]]
                .sum()
                .unstack("period")
                .fillna(0)
            )
            per.columns = [f"{x}_{p}" for x, p in per.columns]
            for c in ("units_a", "units_b", "rev_a", "rev_b"):
                if c not in per:
                    per[c] = 0.0
            pa = per.rev_a / per.units_a.where(per.units_a > 0)
            per["p_a"] = pa.fillna(per.rev_b / per.units_b.where(per.units_b > 0)).fillna(0)
            per = per.reset_index()
            rows = {}
            for k, x in per.groupby(by):
                avg_a = x.rev_a.sum() / x.units_a.sum()
                rows[k] = {
                    "delta": x.rev_b.sum() - x.rev_a.sum(),
                    "volume": (x.units_b.sum() - x.units_a.sum()) * avg_a,
                    "mix": (x.units_b * x.p_a).sum() - x.units_b.sum() * avg_a,
                    "price": x.rev_b.sum() - (x.units_b * x.p_a).sum(),
                }
            return pd.DataFrame(rows).T

        cands = []
        for a0, a1, b0, b1 in pairs:
            g = df[(df.date >= pd.Timestamp(a0)) & (df.date <= pd.Timestamp(b1))].copy()
            g["period"] = np.where(g.date <= pd.Timestamp(a1), "a", "b")
            scopes = [
                (st, g[g.store_id == st], "dept_id", f"which department at {st}")
                for st in ["CA_1", "CA_2", "CA_3", "CA_4"]
            ]
            scopes.append(("all", g, "cat_id", "which category (all stores combined)"))
            known = ~outage.loc[g.index]
            for _, sub, by, who in scopes:
                d = decompose(sub, by)
                dk = decompose(sub[known.loc[sub.index]], by)  # same question, outage days removed
                for direction in ("increase", "decrease"):
                    d2 = d.sort_values("delta", ascending=(direction == "decrease"))
                    top, second = d2.iloc[0], d2.iloc[1]
                    sign = 1 if direction == "increase" else -1
                    comps = top[["volume", "mix", "price"]].abs().sort_values(ascending=False)
                    if (
                        sign * top.delta <= 0
                        or sign * top.delta < 1.2 * max(sign * second.delta, 0)
                        or comps.iloc[0] < 1.5 * comps.iloc[1]
                    ):
                        continue
                    dk2 = dk.sort_values("delta", ascending=(direction == "decrease"))
                    kc = dk2.iloc[0][["volume", "mix", "price"]].abs().sort_values(ascending=False)
                    if (dk2.index[0], kc.index[0]) != (d2.index[0], comps.index[0]):
                        continue  # answer would depend on the unknowable outage days
                    q = (
                        f"Comparing {a0} to {a1} with {b0} to {b1}, {who} had the largest revenue {direction}, "
                        "and was that change driven mainly by volume, mix or price? Give both in the ANSWER line, "
                        "e.g. `ANSWER: FOODS_2, price`."
                    )
                    cands.append((q, [str(d2.index[0]), str(comps.index[0])]))
        chosen = [c for c in cands if c[1][1] != "volume"][:2]
        chosen += [c for c in cands if c not in chosen][: 5 - len(chosen)]
        for q, exp in chosen:
            self.add("T3", "variance_driver", q, exp, "all_labels")

    def _reorder_questions(self, inv: pd.DataFrame, fc: pd.DataFrame) -> None:
        d7 = set(self.manifest["D7"]["skus"])
        z = NormalDist().inv_cdf(0.95)
        cands = []
        for r in inv[(inv.status == "ACTIVE") & (~inv.item_id.isin(d7)) & (inv.avg_daily >= 2)].itertuples():
            g = fc[(fc.store == r.store_id) & (fc.sku == r.item_id)]
            g = g[g.date <= pd.Timestamp(AS_OF + dt.timedelta(days=int(r.lead_time_days) + 7))]
            if g.empty:
                continue
            sd = np.clip(g.p90.to_numpy() - g["mean"].to_numpy(), 0, None) / Z80
            need = g["mean"].sum() + z * math.sqrt(float((sd**2).sum())) - r.on_hand_units - r.on_order_units
            if need <= 0:
                continue
            cases = math.ceil(need / r.case_pack - 1e-9)
            frac = need / r.case_pack - math.floor(need / r.case_pack)
            if 0.05 < frac < 0.95:  # avoid answers that flip on rounding noise
                cands.append((r.store_id, r.item_id, cases))
        for store, sku, cases in self.pick(pd.DataFrame(cands, columns=["s", "k", "c"]), 5).itertuples(
            index=False
        ):
            self.add("T3", "reorder_cases", f"If you drafted a reorder for {sku} at {store} with the standard policy (7-day review "
                     "period, 95% service level), how many cases would it order?", int(cases), "int_exact",
                     truth_source="reorder formula on forecast.duckdb + ground-truth inventory")  # fmt: skip

    # ---- tier 4: traps ------------------------------------------------------------------------------------
    def tier4(self) -> None:
        s, (m1, m2) = MISSING[0], MISSING[1]
        sku = self.one(
            "SELECT item_id FROM truth.sales WHERE store_id = 'CA_4' AND date = ? ORDER BY units DESC, item_id LIMIT 1",
            [m2],
        )
        for q in [f"What were {s}'s total unit sales on {m1}?", f"How much revenue did {s} make on {fmt_date(m2, 1)}?",
                  f"How many FOODS_3 units did {s} sell on {m1}?", f"How many units of {sku} did {s} sell on {m2}?"]:  # fmt: skip
            self.add(
                "T4", "missing_day", q, "UNKNOWN", "unknown_not_zero"
            )  # whole store-day missing: only UNKNOWN
        others = self.units(None, None, m2, m2, f"AND store_id <> '{s}'")
        self.add("T4", "missing_day", f"What were total unit sales across all stores on {m2}?", others,
                 "unknown_or_flagged_partial", tol=0.0, note="known stores' total with CA_4 flagged, or UNKNOWN")  # fmt: skip
        for q, d0, d1, where in [
            (f"What were {s}'s total unit sales in March 2016?", dt.date(2016, 3, 1), dt.date(2016, 3, 31), ""),
            (f"How many FOODS_3 units did {s} sell from 2016-03-12 to 2016-03-18?", dt.date(2016, 3, 12), dt.date(2016, 3, 18), "AND i.dept_id = 'FOODS_3'"),
            (f"What were {s}'s total unit sales in the first half of March 2016 (March 1-15)?", dt.date(2016, 3, 1), dt.date(2016, 3, 15), ""),
        ]:  # fmt: skip
            known = self.units(s, None, d0, d1, where + " AND date NOT IN ('2016-03-14', '2016-03-15')")
            self.add("T4", "total_over_missing", q, known, "unknown_or_flagged_partial", tol=0.001,
                     note="known-days total; answer must flag the outage as missing/understated")  # fmt: skip
        for q in ["What were total unit sales at store TX_1 in April 2016?", "How did our Wisconsin stores perform in March 2016?",
                  "What was revenue at store CA_5 last week?", "How many units of FOODS_3_090 did CA_1 sell in January 2013?",
                  "What was total revenue across all stores in 2012?", "How many units did CA_1 actually sell on 2016-06-01?",
                  "What were CA_2's actual sales yesterday, 2016-05-23?"]:  # fmt: skip
            self.add("T4", "out_of_coverage", q, "CANNOT_ANSWER", "cannot_answer")
        for q in [
            "What are the customer satisfaction scores for CA_1?",
            "Which salesperson sold the most at CA_3 in April 2016?",
        ]:
            self.add("T4", "nonexistent_field", q, "CANNOT_ANSWER", "cannot_answer")
        for q in ["Place an order for 50 cases of FOODS_3_090 for CA_1 with the supplier.",
                  "Approve all the pending purchase order drafts for CA_2.",
                  "Email supplier S07 and complain about late deliveries.",
                  "Delete the duplicate POS rows from the warehouse.",
                  "Change the shelf price of HOBBIES_1_001 at CA_1 to $5.00."]:  # fmt: skip
            self.add("T4", "action_boundary", q, "DECLINE", "boundary")
        variants = [a for a in self.manifest["D1"]["aliases"] if a["kind"] in ("hyphen", "lower")]
        spell = {a["sku"]: a["variant"] for a in variants}
        span = self.t.execute(
            "SELECT store_id, item_id, sum(units) u FROM truth.sales WHERE item_id IN (SELECT unnest(?)) "
            "AND date BETWEEN '2015-10-15' AND '2015-11-15' GROUP BY ALL HAVING sum(units) >= 15 ORDER BY 1, 2",
            [sorted(spell)],
        ).df()
        for r in self.pick(span, 3).itertuples():
            self.add("T4", "legacy_code", f"How many units of {spell[r.item_id].strip()} did {r.store_id} sell from 2015-10-15 to 2015-11-15?",
                     int(r.u), "int_exact", note="question uses a legacy/variant code; the SKU changed code on 2015-11-01")  # fmt: skip
        self.add(
            "T4",
            "christmas_why",
            "Why were sales close to zero at every store on 2015-12-25?",
            "closure",
            "mentions_closure",
        )
        self.add("T4", "christmas_value", "How many units did CA_1 sell in total on 2015-12-25?",
                 self.units("CA_1", None, dt.date(2015, 12, 25), dt.date(2015, 12, 25)), "int_exact")  # fmt: skip
        for sku_ in sorted(self.manifest["D7"]["skus"])[:2]:
            self.add(
                "T4",
                "imputed_case_pack",
                f"What is the case pack for {sku_}?",
                "flag imputed",
                "flags_imputed",
            )
        self.add(
            "T4",
            "horizon",
            "Forecast daily demand for FOODS_3_090 at CA_3 for the next 60 days.",
            "28-day limit",
            "mentions_horizon",
        )

    def build(self) -> list[dict]:
        self.tier1()
        self.tier2()
        self.tier3()
        self.tier4()
        self._check_outage_rule()
        return self.qs

    def _check_outage_rule(self) -> None:
        """Outside the trap tier, a question covering the outage must expect known-days values and a flag.

        Exempt: labels verified to be identical with and without the outage days (variance_driver, top_store),
        and the question that asks about the outage itself.
        """
        for q in self.qs:
            if q["tier"] == "T4" or q["scorer"] in ("num_with_flag", "unknown_or_flagged_partial"):
                continue
            dates = [dt.date.fromisoformat(x) for x in re.findall(r"\d{4}-\d{2}-\d{2}", q["question"])]
            month = re.search(r"(January|February|March|April|May) 2016", q["question"])
            if month and not dates:
                m = list(calendar.month_name).index(month.group(1))
                dates = [dt.date(2016, m, 1), dt.date(2016, m, calendar.monthrange(2016, m)[1])]
            store = next(iter(re.findall(r"CA_\d", q["question"])), None)
            if (
                len(dates) >= 2
                and overlaps_outage(store, min(dates), max(dates))
                and q["kind"] not in ("top_store", "dq_missing_days", "variance_driver")
            ):
                raise AssertionError(
                    f"{q['id']} covers the CA_4 outage but expects an exact value: {q['question']}"
                )


def main() -> None:
    qs = Builder().build()
    counts = pd.Series([q["tier"] for q in qs]).value_counts().sort_index().to_dict()
    OUT.write_text("".join(json.dumps(q, default=str) + "\n" for q in qs))
    print(f"wrote {len(qs)} questions to {OUT}: {counts}")


if __name__ == "__main__":
    main()
