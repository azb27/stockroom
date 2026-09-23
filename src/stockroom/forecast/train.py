"""Train the demand model, backtest it against the customer's current method, and batch-score.

    python -m stockroom.forecast.train             # backtest + final model + 28-day forecasts
    python -m stockroom.forecast.train --backtest  # backtest only
    python -m stockroom.forecast.train --final     # final model only (reuses best iterations)

Backtest protocol (no peeking):
  origin o = as-of - 28 days. Early stopping uses the 28 days before o as validation (models
  trained up to o - 28). Models are then refit up to o with those iteration counts and scored on
  (o, o + 28], with prices frozen at o, exactly as they would be in production.

Baseline = the customer's method: average of the same weekday over the 4 weeks before o.

Outputs: data/forecast.duckdb (forecasts, backtest metrics, model metadata), models/*.txt, and
docs/results/forecast_backtest.md (generated; do not edit by hand).
"""

from __future__ import annotations

import argparse
import datetime as dt
import gc
import json
import time

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd

from stockroom.config import FORECAST_DB, HORIZON_DAYS, MODEL_DIR, ROOT
from stockroom.forecast.features import CATEGORICAL, FEATURES, build_frame
from stockroom.stats import bootstrap_ci, wape
from stockroom.tools.base import as_of, warehouse

TRAIN_DAYS = 330  # training window length (fixed for backtest and final, so they're comparable)
BASE = dict(
    learning_rate=0.08,
    num_leaves=127,
    min_data_in_leaf=100,
    feature_fraction=0.8,
    bagging_fraction=0.5,
    bagging_freq=1,
    max_bin=127,
    lambda_l2=1.0,
    num_threads=2,
    verbose=-1,
    seed=27,
    deterministic=True,
    force_col_wise=True,
)
OBJECTIVES = {
    "mean": dict(objective="tweedie", tweedie_variance_power=1.1),
    "p10": dict(objective="quantile", alpha=0.1),
    "p90": dict(objective="quantile", alpha=0.9),
}
# Quantile models learn demand relative to the series' recent level, so one model can put P10/P90
# in the right place for a SKU selling 0.2/day and one selling 200/day (unscaled, they collapse
# toward the global quantiles: P10 ~ 2 for a 100/day SKU).
SCALED = {"p10", "p90"}
MAX_ROUNDS, PATIENCE = 1200, 50
RESULTS_MD = ROOT / "docs" / "results" / "forecast_backtest.md"
Z80 = 1.2816  # P10..P90 spans +/- 1.2816 sd of a normal


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _frame(con, start: dt.date, end: dt.date, cutoff: dt.date) -> pd.DataFrame:
    return build_frame(con, start, end, price_cutoff=cutoff)


def _scale(df: pd.DataFrame) -> np.ndarray:
    return df["rm28"].fillna(0).to_numpy(dtype=float) + 1.0


def _dataset(df: pd.DataFrame, ref: lgb.Dataset | None = None) -> tuple[lgb.Dataset, np.ndarray, np.ndarray]:
    df = df[df["units"].notna()]
    ds = lgb.Dataset(
        df[FEATURES],
        df["units"],
        categorical_feature=CATEGORICAL,
        reference=ref,
        free_raw_data=True,
        params={"max_bin": BASE["max_bin"], "verbose": -1},
    ).construct()
    return ds, df["units"].to_numpy(dtype=float), _scale(df)


def _set_label(d: tuple[lgb.Dataset, np.ndarray, np.ndarray], name: str) -> lgb.Dataset:
    ds, y, s = d
    ds.set_label(y / s if name in SCALED else y)
    return ds


def _predict(models: dict[str, lgb.Booster], df: pd.DataFrame) -> pd.DataFrame:
    out = df[["store", "sku", "date"]].copy()
    for name, m in models.items():
        raw = m.predict(df[FEATURES], num_threads=2)
        out[name] = np.clip(raw * _scale(df) if name in SCALED else raw, 0, None)
    out["ordering_fixed"] = (out["p10"] > out["mean"]) | (out["p90"] < out["mean"])
    out["p10"] = np.minimum(out["p10"], out["mean"])
    out["p90"] = np.maximum(out["p90"], out["mean"])
    return out


def find_iterations(con) -> dict[str, int]:
    o = as_of() - dt.timedelta(days=HORIZON_DAYS)
    vo = o - dt.timedelta(days=HORIZON_DAYS)
    _log(f"early stopping: train to {vo}, validate {vo + dt.timedelta(1)}..{o}")
    dtr = _dataset(_frame(con, vo - dt.timedelta(TRAIN_DAYS), vo, vo))
    dva = _dataset(_frame(con, vo + dt.timedelta(1), o, vo), ref=dtr[0])
    best = {}
    for name, obj in OBJECTIVES.items():
        t = time.time()
        m = lgb.train(
            {**BASE, **obj},
            _set_label(dtr, name),
            MAX_ROUNDS,
            valid_sets=[_set_label(dva, name)],
            callbacks=[lgb.early_stopping(PATIENCE, verbose=False)],
        )
        best[name] = int(m.best_iteration or MAX_ROUNDS)
        _log(f"  {name}: best iteration {best[name]} ({time.time() - t:.0f}s)")
    del dtr, dva
    gc.collect()
    return best


def fit(con, end: dt.date, iters: dict[str, int]) -> dict[str, lgb.Booster]:
    _log(f"fit: {end - dt.timedelta(TRAIN_DAYS)}..{end}")
    dtr = _dataset(_frame(con, end - dt.timedelta(TRAIN_DAYS), end, end))
    models = {}
    for name, obj in OBJECTIVES.items():
        t = time.time()
        models[name] = lgb.train({**BASE, **obj}, _set_label(dtr, name), iters[name])
        _log(f"  {name}: {iters[name]} rounds ({time.time() - t:.0f}s)")
    del dtr
    gc.collect()
    return models


def baseline(con, origin: dt.date, test: pd.DataFrame) -> np.ndarray:
    """Same weekday, averaged over the 4 weeks up to the origin (unknown days skipped)."""
    hist = con.execute(
        """SELECT s.store, s.sku, d.date, CASE WHEN st.status = 'missing_data' THEN NULL
                  ELSE coalesce(f.units, 0) END AS units
           FROM (SELECT DISTINCT store, sku FROM core.fact_sales_daily) s
           CROSS JOIN (SELECT date FROM core.dim_date WHERE date > ?::DATE - 28 AND date <= ?) d
           LEFT JOIN core.fact_sales_daily f USING (store, sku, date)
           LEFT JOIN core.store_day_status st ON st.store = s.store AND st.date = d.date""",
        [origin, origin],
    ).df()
    hist["dow"] = pd.to_datetime(hist["date"]).dt.dayofweek
    b = hist.groupby(["store", "sku", "dow"])["units"].mean().rename("baseline").reset_index()
    t = test[["store", "sku", "date"]].copy()
    t["dow"] = pd.to_datetime(t["date"]).dt.dayofweek
    return t.merge(b, on=["store", "sku", "dow"], how="left")["baseline"].fillna(0).to_numpy()


def evaluate(con, origin: dt.date, test: pd.DataFrame, pred: pd.DataFrame) -> dict:
    df = pred.copy()
    df["y"] = test["units"].to_numpy()
    df["base"] = baseline(con, origin, test)
    df = df[df["y"].notna()]
    meta = con.execute("SELECT sku, dept, category FROM core.dim_sku").df()
    vel = con.execute(
        """SELECT store, sku, sum(units) / 28.0 AS v FROM core.fact_sales_daily
           WHERE date > ?::DATE - 28 AND date <= ? GROUP BY ALL""",
        [origin, origin],
    ).df()
    df = df.merge(meta, on="sku").merge(vel, on=["store", "sku"], how="left")
    df["v"] = df["v"].fillna(0)
    df["velocity"] = pd.cut(
        df["v"], [-1, 1, 5, np.inf], labels=["slow (<1/day)", "medium (1-5/day)", "fast (>5/day)"]
    )

    rows = []

    def add(level: str, seg: str, d: pd.DataFrame) -> None:
        y = d["y"].to_numpy()
        rows.append(
            {
                "level": level,
                "segment": seg,
                "rows": len(d),
                "units": float(y.sum()),
                "wape_model": wape(y, d["mean"].to_numpy()),
                "wape_baseline": wape(y, d["base"].to_numpy()),
                "bias_model": float(d["mean"].sum() / y.sum() - 1),
                "bias_baseline": float(d["base"].sum() / y.sum() - 1),
                "below_p10": float(np.mean(y < d["p10"].to_numpy())),
                "above_p90": float(np.mean(y > d["p90"].to_numpy())),
            }
        )

    add("store-SKU-day", "all", df)
    for col in ("category", "store", "velocity"):
        for seg, d in df.groupby(col, observed=True):
            add(f"store-SKU-day by {col}", str(seg), d)
    agg = df.groupby(["store", "dept", "date"], observed=True)[["y", "mean", "base"]].sum()
    rows.append(
        {
            "level": "store-dept-day",
            "segment": "all",
            "rows": len(agg),
            "units": float(agg["y"].sum()),
            "wape_model": wape(agg["y"].to_numpy(), agg["mean"].to_numpy()),
            "wape_baseline": wape(agg["y"].to_numpy(), agg["base"].to_numpy()),
            "bias_model": float(agg["mean"].sum() / agg["y"].sum() - 1),
            "bias_baseline": float(agg["base"].sum() / agg["y"].sum() - 1),
            "below_p10": None,
            "above_p90": None,
        }
    )

    # Is the model better, or did we get lucky with these series? Bootstrap over store-SKU series.
    per = df.assign(em=(df["y"] - df["mean"]).abs(), eb=(df["y"] - df["base"]).abs())
    per = per.groupby(["store", "sku"])[["em", "eb", "y"]].sum()
    em, eb, yy = per["em"].to_numpy(), per["eb"].to_numpy(), per["y"].to_numpy()
    point, lo, hi = bootstrap_ci(lambda i: (eb[i].sum() - em[i].sum()) / yy[i].sum(), len(per))
    return {
        "metrics": pd.DataFrame(rows),
        "improvement_pp": {"point": point * 100, "ci95": [lo * 100, hi * 100], "series": len(per)},
        "ordering_fixed_share": float(pred["ordering_fixed"].mean()),
    }


def write_results(origin: dt.date, iters: dict[str, int], ev: dict) -> None:
    m = ev["metrics"]
    imp = ev["improvement_pp"]
    fmt = lambda x: "n/a" if x is None or pd.isna(x) else f"{x:.1%}"  # noqa: E731
    lines = [
        "# Forecast backtest",
        "",
        "Generated by `python -m stockroom.forecast.train --backtest`. Do not edit by hand.",
        "",
        (
            f"- **Origin:** {origin}. Train through the origin, forecast the next {HORIZON_DAYS} days "
            f"({origin + dt.timedelta(1)} to {origin + dt.timedelta(HORIZON_DAYS)})."
        ),
        (
            f"- **Model:** one global LightGBM across all store-SKU series: Tweedie for the mean, quantile "
            f"models for P10/P90. Every feature looks at least {HORIZON_DAYS} days back, and prices are "
            f"frozen at the origin. Boosting rounds (early stopping on the 28 days before the origin): "
            f"mean {iters['mean']}, P10 {iters['p10']}, P90 {iters['p90']}."
        ),
        (
            "- **Baseline:** the customer's current method, the average of the same weekday over the "
            "4 weeks before the origin."
        ),
        (
            "- **WAPE** = sum|actual - forecast| / sum actual (lower is better). **Bias** = sum forecast / "
            "sum actual - 1. **Below P10 / above P90** = share of actual days under the P10 or over the P90 "
            "forecast; a calibrated model has at most 10% each. Above-P90 is the one safety stock depends on."
        ),
        "",
        (
            f"**Headline:** at store-SKU-day level the model's WAPE is **{abs(imp['point']):.1f} pp "
            f"{'lower' if imp['point'] > 0 else 'higher'}** than the baseline "
            f"(improvement {imp['point']:+.1f} pp, 95% CI [{imp['ci95'][0]:+.1f}, {imp['ci95'][1]:+.1f}] pp, "
            f"bootstrap over {imp['series']:,} store-SKU series; "
            f"{'significant' if imp['ci95'][0] > 0 or imp['ci95'][1] < 0 else 'not significant'} at 5%)."
        ),
        "",
        "| Level | Segment | Units | WAPE model | WAPE baseline | Bias model | Bias baseline | Below P10 | Above P90 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in m.itertuples():
        lines.append(
            f"| {r.level} | {r.segment} | {r.units:,.0f} | {fmt(r.wape_model)} | {fmt(r.wape_baseline)} | "
            f"{r.bias_model:+.1%} | {r.bias_baseline:+.1%} | {fmt(r.below_p10)} | {fmt(r.above_p90)} |"
        )
    overall = m[(m.level == "store-SKU-day") & (m.segment == "all")].iloc[0]
    losers = m[(m.level.str.startswith("store-SKU-day by")) & (m.wape_model > m.wape_baseline)]
    agg = m[m.level == "store-dept-day"].iloc[0]
    lines += [
        "",
        "## Reading this honestly",
        (
            f"- Demand exceeded the P90 on {overall.above_p90:.1%} of store-SKU-days (target 10%): the P90 is "
            f"{'slightly low, so safety stock is slightly understated' if overall.above_p90 > 0.10 else 'conservative'}. "
            "Reorder safety stock uses the P90 side only."
        ),
        *(
            [
                (
                    f"- The model is worse than the baseline for: {', '.join(f'{r.segment} ({r.wape_model:.1%} vs {r.wape_baseline:.1%})' for r in losers.itertuples())}. "
                    "For those series the simple rule is as good or better."
                )
            ]
            if len(losers)
            else []
        ),
        (
            f"- At store-dept-day level the gain is small ({agg.wape_model:.1%} vs {agg.wape_baseline:.1%}): "
            "averaging the same weekday is a strong baseline once SKUs are summed. The model earns its keep at SKU level, "
            "where orders are placed."
        ),
        "- Store-SKU-day WAPE is high for any method because most series sell 0-2 units a day.",
        (
            "- One origin and 28 days is one draw. The bootstrap covers series-to-series noise, not "
            "the chance that this particular month was easy or hard. Rolling-origin backtests are on the "
            "week-2 plan."
        ),
        (
            f"- P10 above the mean or P90 below it happened on {ev['ordering_fixed_share']:.1%} of "
            "predictions; those were clipped so P10 <= mean <= P90."
        ),
        "- This is not comparable to M5 leaderboard scores (different subset, horizon handling and metric).",
    ]
    RESULTS_MD.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_MD.write_text("\n".join(lines) + "\n")
    _log(f"wrote {RESULTS_MD}")


def _store(tables: dict[str, pd.DataFrame], replace: bool = True) -> None:
    con = duckdb.connect(str(FORECAST_DB))
    for name, df in tables.items():
        con.register("_df", df)
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _df")
        con.unregister("_df")
    con.close()


def _meta() -> dict:
    if not FORECAST_DB.exists():
        return {}
    con = duckdb.connect(str(FORECAST_DB), read_only=True)
    try:
        return dict(con.execute("SELECT key, value FROM model_meta").fetchall())
    except duckdb.CatalogException:
        return {}
    finally:
        con.close()


def run_backtest(con) -> dict[str, int]:
    o = as_of() - dt.timedelta(days=HORIZON_DAYS)
    iters = find_iterations(con)
    models = fit(con, o, iters)
    test = _frame(con, o + dt.timedelta(1), o + dt.timedelta(HORIZON_DAYS), o)
    pred = _predict(models, test)
    ev = evaluate(con, o, test, pred)
    write_results(o, iters, ev)
    m = ev["metrics"]
    _store({"backtest_metrics": m})
    meta = _meta() | {
        "best_iterations": json.dumps(iters),
        "backtest_origin": str(o),
        "backtest_improvement_pp": json.dumps(ev["improvement_pp"]),
    }
    _store({"model_meta": pd.DataFrame(list(meta.items()), columns=["key", "value"])})
    print(m[["level", "segment", "wape_model", "wape_baseline", "below_p10", "above_p90"]].to_string())
    return iters


def run_final(con, iters: dict[str, int]) -> None:
    end = as_of()
    models = fit(con, end, iters)
    version = f"lgbm-{end:%Y%m%d}-{time.strftime('%Y%m%d%H%M')}"
    MODEL_DIR.mkdir(exist_ok=True)
    for name, m in models.items():
        m.save_model(str(MODEL_DIR / f"{version}_{name}.txt"))
    fut = _frame(con, end + dt.timedelta(1), end + dt.timedelta(HORIZON_DAYS), end)
    pred = _predict(models, fut).drop(columns="ordering_fixed")
    pred["date"] = pd.to_datetime(pred["date"]).dt.date
    meta = _meta() | {
        "model_version": version,
        "trained_through": str(end),
        "horizon_days": str(HORIZON_DAYS),
        "features": json.dumps(FEATURES),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _store({"forecast": pred, "model_meta": pd.DataFrame(list(meta.items()), columns=["key", "value"])})
    _log(f"scored {len(pred):,} store-SKU-days as {version}")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--backtest", action="store_true")
    g.add_argument("--final", action="store_true")
    args = ap.parse_args()
    con = warehouse().cursor()
    t0 = time.time()
    if args.final:
        iters = json.loads(_meta().get("best_iterations", "null") or "null")
        if not iters:
            raise SystemExit("no best_iterations yet: run with --backtest first")
    else:
        iters = run_backtest(con)
    if not args.backtest:
        run_final(con, iters)
    _log(f"done in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
