# Discovery memo: Larkspur Distribution (fictional)

*Written as the first deliverable of a forward-deployed engagement: what the customer said, what we found in their data, and what we agreed to build. The customer is fictional; the data patterns are real (M5) or deliberately injected (see ADR 0001).*

## Who they are
- Regional distributor supplying 4 retail accounts (CA_1 to CA_4), ~3,000 active SKUs across Foods, Household and Hobbies, and 17 suppliers with lead times of 3–14 days.
- 2 ops analysts and 1 buyer. No data team.

## What they told us (pain → cost)
| # | Pain, in their words | Today's workaround | Cost |
|---|---|---|---|
| P1 | "Every question is an Excel export." | ERP export, pivot table, email | ~1 analyst-day/week |
| P2 | "We find stock-outs when the store calls us." | Reactive rush orders | Lost sales plus expedite fees |
| P3 | "Nobody trusts the numbers since the migration." | Manual reconciliation | Decisions delayed or made on gut |
| P4 | "Reordering is the buyer's spreadsheet and memory." | Buyer's personal model | Key-person risk, overstock |
| P5 | "When revenue drops we can't say why for a week." | Ad-hoc analysis | Slow response to price or mix changes |

## What we found in the data (week 1)
From the data-quality pass (`core.dq_issues`):

| Finding | Scale | Business impact |
|---|---|---|
| ERP migration (2015-11-01) re-coded 25 SKUs; POS sends lowercase/space variants for 15 more | 40 codes, 13.5k sales lines | Sales for migrated SKUs look like they fell to ~0 and "new" SKUs appeared. This is the root of P3. |
| CA_3 logged some FOODS_3 items in cases (Feb–Mar 2016), labelled `CS`/`cs`/`Case` | 603 lines | Those SKUs' sales under-reported by 12–24× |
| One weekly POS file for CA_2 loaded twice | 10.5k lines | That week's CA_2 sales double-counted |
| CA_4 POS feed dropped 2016-03-14 and 03-15 | 2 store-days | Reads as zero sales. Must be treated as unknown. |
| 25 price-book entries keyed in cents | 25 rows | Revenue spikes of 100× on those weeks |
| Missing price-book weeks | ~18.5k store-SKU-weeks | Revenue gaps; forward-filled and flagged |
| 8 SKUs with no case pack in master | 8 SKUs | Can't convert cases or build orders without a rule |

Christmas (2014-12-25, 2015-12-25) shows near-zero sales at every store. That's a closure, not a data problem, and the pipeline labels it as such.

## What we agreed to build (scope)
| Pain | Capability | Tool |
|---|---|---|
| P1 | Plain-English questions over cleaned data, with caveats | `describe_data`, `run_sql` |
| P2 | Stock-out and overstock alerts | `detect_anomalies(scope='inventory')` |
| P3 | Cleaned layer + visible data-quality log | `core.*`, `core.dq_issues` |
| P4 | Forecast-driven draft POs a buyer approves | `forecast_demand`, `draft_reorder` |
| P5 | Revenue change decomposed into volume / price / mix | `explain_variance` |

**Explicitly not in scope:** sending orders to suppliers, changing prices, integrating with the live ERP (phase 2 of the engagement), multi-user auth.

## Success criteria (agreed with the customer)
1. ≥ 85% of ops questions from their real backlog answered correctly (measured by `evals/`, with a confidence interval). *Met in P5: Sonnet 5 120/120; Haiku 4.5 96% [92, 99] (`docs/results/eval.md`).*
2. Zero cases of the agent reporting a missing day as zero sales. *Met in P5: 9 questions touch the outage days, and every cleaned-layer run (4 runs, 36 answers) flagged them as unknown.*
3. Buyer approves or edits every PO. No autonomous ordering.
4. Forecast beats their current method (same-weekday average of the last 4 weeks) on WAPE. *Met in the P3 backtest: 73.2% vs 76.8% at SKU level, 9.2% vs 9.9% at store-dept level. Slow sellers are a tie (see `docs/results/forecast_backtest.md`).*
