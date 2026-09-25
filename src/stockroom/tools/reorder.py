"""draft_reorder: turn forecasts + inventory into per-supplier draft POs a buyer must approve.

Policy (periodic review, order-up-to), per SKU at one store:

    H       = supplier lead time L + review period R         (days until the *next* order arrives)
    demand  = sum of forecast means over the next H days
    sigma   = sqrt(sum of daily variances), daily sd = (P90 - mean) / 1.2816
              (upper tail only: running out is what safety stock protects against, and the P90
              is the better-calibrated quantile in the backtest)
    safety  = z(service level) x sigma
    need    = demand + safety - on_hand - on_order
    order   = ceil(need / case_pack) whole cases, if need > 0

Supplier minimums are per purchase order, so a draft below the minimum is flagged for the buyer,
never silently inflated.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import uuid
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
import pandas as pd

from stockroom.appdb import app_session
from stockroom.config import HORIZON_DAYS
from stockroom.tools.base import ToolError, ToolResult, as_of, resolve_sku, warehouse
from stockroom.tools.forecast import Z80, load_forecast, model_meta

MAX_LINES_RETURNED = 30


@dataclass(frozen=True)
class OrderCalc:
    demand: float
    sigma: float
    safety: float
    target: float
    need: float
    cases: int
    units: int


def order_quantity(
    mean: np.ndarray, p90: np.ndarray, on_hand: float, on_order: float, case_pack: int, z: float
) -> OrderCalc:
    """Pure order-up-to calculation over the days in `mean` (already cut to lead + review)."""
    if case_pack <= 0:
        raise ValueError("case_pack must be positive")
    demand = float(np.sum(mean))
    sd = np.clip(np.asarray(p90) - np.asarray(mean), 0, None) / Z80
    sigma = math.sqrt(float(np.sum(sd**2)))
    safety = z * sigma
    target = demand + safety
    need = target - on_hand - on_order
    cases = math.ceil(need / case_pack - 1e-9) if need > 0 else 0
    return OrderCalc(demand, sigma, safety, target, need, cases, cases * case_pack)


def draft_reorder(
    store: str,
    dept: str | None = None,
    skus: list[str] | None = None,
    review_days: int = 7,
    service_level: float = 0.95,
) -> ToolResult:
    """Draft POs (one per supplier) for a store. Drafts are PENDING_APPROVAL; nothing is sent."""
    if not 0.5 <= float(service_level) <= 0.995:
        raise ToolError("service_level must be between 0.5 and 0.995")
    review_days = int(review_days)
    if not 1 <= review_days <= 14:
        raise ToolError("review_days must be 1-14")
    cur = warehouse().cursor()
    caveats: list[str] = []
    scope_sql, params = "", [store]
    if skus:
        resolved = []
        for s in skus:
            r, c = resolve_sku(cur, s)
            resolved.append(r)
            caveats += c
        scope_sql = " AND i.sku IN (SELECT unnest(?))"
        params.append(resolved)
    if dept:
        scope_sql += " AND k.dept = ?"
        params.append(dept)
    inv = cur.execute(
        f"""SELECT i.sku, k.dept, i.on_hand_units, i.on_order_units, k.case_pack, k.case_pack_imputed,
                   k.unit_cost, k.supplier_id, s.lead_time_days, s.min_order_cases
            FROM core.inventory i JOIN core.dim_sku k USING (sku) JOIN core.dim_supplier s USING (supplier_id)
            WHERE i.store = ? AND k.status = 'ACTIVE' {scope_sql}""",
        params,
    ).df()
    if inv.empty:
        raise ToolError("no active SKUs with inventory match that scope")
    if review_days + int(inv["lead_time_days"].max()) > HORIZON_DAYS:
        raise ToolError(f"lead time + review period exceeds the {HORIZON_DAYS}-day forecast horizon")

    fc = load_forecast(inv["sku"].tolist(), store, HORIZON_DAYS)
    fc["day"] = (pd.to_datetime(fc["date"]) - pd.Timestamp(as_of())).dt.days
    by_sku = {k: g.sort_values("day") for k, g in fc.groupby("sku")}
    z = NormalDist().inv_cdf(float(service_level))

    lines, no_fc = [], []
    for r in inv.itertuples(index=False):
        g = by_sku.get(r.sku)
        if g is None:
            no_fc.append(r.sku)
            continue
        h = int(r.lead_time_days) + review_days
        g = g[g["day"] <= h]
        c = order_quantity(
            g["mean"].to_numpy(), g["p90"].to_numpy(), r.on_hand_units, r.on_order_units, int(r.case_pack), z
        )
        if c.cases > 0:
            lines.append(
                {
                    "sku": r.sku,
                    "supplier_id": r.supplier_id,
                    "on_hand": int(r.on_hand_units),
                    "on_order": int(r.on_order_units),
                    "lead_time_days": int(r.lead_time_days),
                    "review_days": review_days,
                    "forecast_units": round(c.demand, 2),
                    "safety_units": round(c.safety, 2),
                    "target_units": round(c.target, 2),
                    "case_pack": int(r.case_pack),
                    "case_pack_imputed": bool(r.case_pack_imputed),
                    "order_cases": c.cases,
                    "order_units": c.units,
                    "unit_cost": float(r.unit_cost),
                    "est_cost": round(c.units * float(r.unit_cost), 2),
                    "min_order_cases": int(r.min_order_cases),
                }
            )
    if no_fc:
        caveats.append(
            f"{len(no_fc)} SKU(s) have no forecast at {store} (never sold there): {', '.join(no_fc[:10])}"
        )
    if not lines:
        return ToolResult(
            data={"store": store, "drafts": [], "message": "Stock covers the target for every SKU in scope."},
            caveats=caveats,
            provenance={"tables": ["core.inventory", "forecast.forecast"]},
        )

    df = pd.DataFrame(lines)
    meta = model_meta()
    now = dt.datetime.now()
    drafts = []
    params_json = json.dumps(
        {
            "store": store,
            "dept": dept,
            "skus": skus,
            "review_days": review_days,
            "service_level": service_level,
        }
    )
    with app_session() as app:
        app.execute("BEGIN TRANSACTION")  # all of a call's drafts land, or none do
        for sup, g in df.groupby("supplier_id"):
            did = f"PO-{store}-{sup}-{uuid.uuid4().hex[:6].upper()}"
            min_cases = int(g["min_order_cases"].iloc[0])
            below = int(g["order_cases"].sum()) < min_cases
            app.execute(
                "INSERT INTO po_drafts VALUES (?, ?, 'agent', ?, ?, 'PENDING_APPROVAL', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                [
                    did,
                    now,
                    store,
                    sup,
                    len(g),
                    int(g["order_cases"].sum()),
                    int(g["order_units"].sum()),
                    float(g["est_cost"].sum()),
                    below,
                    meta.get("model_version"),
                    params_json,
                ],
            )
            ln = g.drop(columns=["supplier_id", "min_order_cases"]).assign(draft_id=did)
            app.register("_ln", ln)
            app.execute(
                """INSERT INTO po_draft_lines SELECT draft_id, sku, on_hand, on_order, lead_time_days, review_days,
                   forecast_units, safety_units, target_units, case_pack, case_pack_imputed, order_cases,
                   order_units, unit_cost, est_cost FROM _ln"""
            )
            app.unregister("_ln")
            drafts.append(
                {
                    "draft_id": did,
                    "supplier_id": sup,
                    "lines": len(g),
                    "cases": int(g["order_cases"].sum()),
                    "est_cost": round(float(g["est_cost"].sum()), 2),
                    "supplier_min_cases": min_cases,
                    "below_supplier_minimum": below,
                }
            )
            if below:
                caveats.append(
                    f"{did}: {int(g['order_cases'].sum())} cases is below {sup}'s minimum of {min_cases}. "
                    "The buyer should add lines or hold the order; the draft was not inflated."
                )
        app.execute("COMMIT")

    imputed = df.loc[df["case_pack_imputed"], "sku"].tolist()
    if imputed:
        caveats.append(
            f"Case pack was imputed (missing in the SKU master) for {', '.join(imputed)}; confirm with the supplier."
        )
    caveats.append(
        "Drafts are PENDING_APPROVAL. Nothing has been ordered or sent; a buyer must approve each draft."
    )
    top = df.sort_values("est_cost", ascending=False).head(MAX_LINES_RETURNED)
    return ToolResult(
        data={
            "store": store,
            "policy": {
                "review_days": review_days,
                "service_level": service_level,
                "z": round(z, 3),
                "rule": "order up to forecast(lead time + review) + z*sigma, in whole cases",
            },
            "drafts": drafts,
            "totals": {
                "lines": len(df),
                "cases": int(df["order_cases"].sum()),
                "est_cost": round(float(df["est_cost"].sum()), 2),
            },
            "top_lines": top.drop(columns=["supplier_id", "min_order_cases"]).to_dict(orient="records"),
            "lines_shown": len(top),
            "model_version": meta.get("model_version"),
        },
        caveats=caveats,
        provenance={
            "tables": ["core.inventory", "core.dim_sku", "core.dim_supplier", "forecast.forecast"],
            "wrote": "app.po_drafts, app.po_draft_lines",
        },
    )
