"""forecast_demand: read the nightly batch forecast for one SKU (one store or all stores)."""

from __future__ import annotations

import datetime as dt
import math

import pandas as pd

from stockroom import config
from stockroom.config import HORIZON_DAYS
from stockroom.tools.base import ToolError, ToolResult, as_of, resolve_sku, warehouse

Z80 = 1.2816


def forecast_db():
    if not config.FORECAST_DB.exists():
        raise ToolError("no forecasts yet: run `python -m stockroom.forecast.train`")
    return warehouse(config.FORECAST_DB)


def model_meta() -> dict[str, str]:
    return dict(forecast_db().cursor().execute("SELECT key, value FROM model_meta").fetchall())


def load_forecast(skus: list[str], store: str | None, horizon_days: int) -> pd.DataFrame:
    end = as_of() + dt.timedelta(days=horizon_days)
    q = "SELECT store, sku, date, p10, mean, p90 FROM forecast WHERE date <= ? AND sku IN (SELECT unnest(?))"
    params: list = [end, skus]
    if store:
        q += " AND store = ?"
        params.append(store)
    return forecast_db().cursor().execute(q + " ORDER BY sku, store, date", params).df()


def forecast_demand(sku: str, store: str | None = None, horizon_days: int = 14) -> ToolResult:
    """Daily demand forecast (mean with P10-P90 range) for a SKU over the next 1-28 days."""
    horizon_days = int(horizon_days)
    if not 1 <= horizon_days <= HORIZON_DAYS:
        raise ToolError(f"horizon_days must be 1-{HORIZON_DAYS}; the model does not forecast further ahead")
    cur = warehouse().cursor()
    sku, caveats = resolve_sku(cur, sku)
    status, category = cur.execute(
        "SELECT status, category FROM core.dim_sku WHERE sku = ?", [sku]
    ).fetchone()
    fc = load_forecast([sku], store, horizon_days)
    if fc.empty:
        raise ToolError(f"no forecast for {sku}{' at ' + store if store else ''}: it has never sold there")

    # Across stores: sum the means; combine spreads assuming stores are independent.
    fc["sd"] = (fc["p90"] - fc["p10"]) / (2 * Z80)
    daily = (
        fc.groupby("date").agg(mean=("mean", "sum"), var=("sd", lambda s: float((s**2).sum()))).reset_index()
    )
    if store:
        daily = daily.drop(columns="var").merge(fc[["date", "p10", "p90"]], on="date")
    else:
        sd = daily["var"] ** 0.5
        daily["p10"] = (daily["mean"] - Z80 * sd).clip(lower=0)
        daily["p90"] = daily["mean"] + Z80 * sd
        daily = daily.drop(columns="var")
        caveats.append("All-store P10/P90 combine store ranges assuming independence (approximate).")
    daily["date"] = pd.to_datetime(daily["date"]).dt.date
    daily[["mean", "p10", "p90"]] = daily[["mean", "p10", "p90"]].round(2)
    total_mean = float(daily["mean"].sum())
    total_sd = math.sqrt(float((fc["sd"] ** 2).sum()))

    recent = cur.execute(
        f"""SELECT coalesce(sum(units), 0) / 28.0 FROM core.fact_sales_daily
            WHERE sku = ? AND date > ?::DATE - 28 {"AND store = ?" if store else ""}""",
        [sku, as_of(), *([store] if store else [])],
    ).fetchone()[0]
    meta = model_meta()
    bt = (
        forecast_db()
        .cursor()
        .execute(
            """SELECT wape_model, wape_baseline FROM backtest_metrics
           WHERE level = 'store-SKU-day by category' AND segment = ?""",
            [category],
        )
        .fetchone()
    )
    if status != "ACTIVE":
        caveats.append(f"{sku} is {status}; forecast shown for reference only.")
    caveats.append(
        "Forecasts assume current shelf prices continue; promotions or price changes are not known to the model."
    )
    return ToolResult(
        data={
            "sku": sku,
            "store": store or "all stores",
            "horizon": [as_of() + dt.timedelta(1), as_of() + dt.timedelta(horizon_days)],
            "daily": daily[["date", "mean", "p10", "p90"]].to_dict(orient="records"),
            "total_units": {
                "mean": round(total_mean, 1),
                "p10_approx": round(max(0.0, total_mean - Z80 * total_sd), 1),
                "p90_approx": round(total_mean + Z80 * total_sd, 1),
            },
            "recent_avg_daily_units_28d": round(float(recent), 2),
            "model": {
                "version": meta.get("model_version"),
                "trained_through": meta.get("trained_through"),
                f"backtest_wape_{category.lower()}": round(bt[0], 3) if bt else None,
                f"baseline_wape_{category.lower()}": round(bt[1], 3) if bt else None,
            },
        },
        caveats=caveats,
        provenance={"tables": ["forecast.forecast", "forecast.model_meta", "core.fact_sales_daily"]},
    )
