# ADR 0001: DuckDB warehouse, with data-quality issues injected into real data

**Status:** accepted · **Phase:** P1

## Context
The agent needs a realistic "customer warehouse" to work against. Real distributor data is private; clean public datasets don't exercise the hardest part of FDE work, which is messy data. Evals need ground truth that is independent of our own cleaning code.

## Decision
- Real sales, prices and calendar from M5 (CA stores, 730 days). Synthetic ERP attributes (case packs, costs, suppliers, inventory), generated with a fixed seed.
- Keep the untouched version in `ground_truth.duckdb`. Derive `warehouse.duckdb` `raw.*` from it by injecting 7 documented issue classes, with every touched key written to `dirt_manifest.json`.
- The cleaning layer (`core.*`) detects issues from raw data only. Tests compare `core.*` with ground truth.
- DuckDB: embedded, a single file, fast on millions of rows, no server to host. The 41 MB file ships inside the API container.

## Consequences
- We can assert exact recovery (sales rows match truth exactly outside the 2 unrecoverable missing days).
- Eval answers computed from ground truth penalise cleaning mistakes, as a customer would.
- Honest limit: cleaning is proven against known, injected issue classes, not unknown ones. The README must say so.
- DuckDB allows one writer. Agent drafts go to a separate `app` schema, written only by the API process. This is acceptable for a single-instance demo, and would move to Postgres for multi-user.

## Alternatives considered
- **Postgres/Supabase:** a hosted DB adds ops and cost for no demo benefit. Revisit for multi-user.
- **Purely synthetic data:** easy ground truth, but reviewers discount it. Real demand patterns (seasonality, SNAP, Christmas) matter for forecasting credibility.
