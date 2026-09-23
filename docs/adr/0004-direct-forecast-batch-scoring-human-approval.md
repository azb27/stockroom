# ADR 0004: Direct 28-day LightGBM, batch-scored nightly; reorder drafts are per supplier and human-approved

**Status:** accepted · **Phase:** P3

## Context
The buyer needs daily demand for up to lead time + review period (at most 21 days here) for ~12k store-SKU series. The agent needs answers in milliseconds, not a model run per question. The reorder policy must survive a buyer's scrutiny.

## Decision
1. **One global LightGBM model, direct (not recursive).**
   - Every demand feature looks back at least 28 days, so one model covers the whole 28-day horizon without feeding its own predictions back in.
   - A test deletes every sale and price after the origin and asserts the features don't change.
   - A mutation to a 27-day look-back makes that test fail.
2. **Mean from a Tweedie model; P10/P90 from two quantile models.** Tweedie suits intermittent, zero-heavy counts.
   - The quantile models learn demand relative to each series' recent level: target = units / (28-day mean + 1), scaled back at prediction.
   - Unscaled, they collapsed toward global quantiles. A SKU selling ~100/day got P10 ≈ 2, which would have inflated its safety stock.
   - Safety stock uses the upper tail only, σ = (P90 − mean) / 1.2816. Running out is the risk being covered, and P90 is the better-calibrated quantile in the backtest (see `docs/results/forecast_backtest.md`).
3. **Prices after the origin are frozen at the last known price,** in the backtest and in production alike. The model is never told about future promotions it couldn't know.
4. **Batch scoring.** Training writes all 28 days for every series into `forecast.duckdb`. Tools read it through the same locked, read-only connection as the warehouse. Re-scoring is a nightly job, followed by an app restart.
5. **Reorder policy: periodic-review order-up-to,** in whole cases. One draft PO per supplier, status `PENDING_APPROVAL`.
   - Supplier minimums are flagged, never auto-inflated.
   - Approval lives in `stockroom.approvals` (CLI now, UI in P7), outside the tool registry, and needs a named person. Rejection needs a reason.

## Consequences
- The backtest is honest by construction: same features, same frozen prices, early stopping on data before the origin only.
- Forecasts can't react to intra-day news. That's fine for a nightly replenishment cycle; it's wrong for a flash sale.
- Safety stock assumes independent daily errors and a normal-shaped upper tail. That's reasonable for replenishment. The backtest reports how often demand exceeds P90 (target 10%) so a buyer can see whether it's calibrated.
- One backtest origin is one draw. Rolling-origin evaluation is on the week-2 plan.

## Alternatives considered
- **Recursive lag-1 model:** usually sharper for days 1–7, but it compounds error and makes leakage harder to reason about. Revisit if short-horizon accuracy becomes the bottleneck.
- **Per-series statistical models (ETS/Croston):** 12k fits, weak on intermittent series, and no cross-learning between SKUs.
- **On-demand inference inside the tool:** fresher, but slower and harder to audit. "What did the model say on Tuesday" is a row in a table, not a re-run.
- **Auto-rounding drafts up to supplier minimums:** convenient, but it silently buys stock nobody asked for. That is the buyer's call.
