# Stockroom

**An ops agent for a distributor's messy data: it answers questions, forecasts demand, flags stock-outs, and drafts purchase orders that a human approves. It's measured against 120 ground-truth questions with confidence intervals.**

> Status: **Phase 1 of 8 complete** (data layer). Plan of record: [`SPEC.md`](SPEC.md). Live demo, eval table and demo video land in P5–P8.

## Why this exists
Most agent demos run on clean data. Real deployments fail on messy data: half-finished ERP migrations, units in the wrong field, files loaded twice, outages that look like zero sales. Stockroom is built like a forward-deployed engagement:
1. A [discovery memo](docs/engagement/discovery-memo.md).
2. A data-quality pass.
3. Tools that each trace to a customer pain point.
4. An eval that says how often the agent is right, with error bars.

## Phase 1: the data layer
Real Walmart sales (M5, 4 California stores, 3,049 SKUs, 730 days, 3.65M sales lines) plus seeded synthetic ERP attributes. Seven classes of real-world data problems are injected into it; the cleaning layer finds all seven **from the raw data alone**.

| Issue found by the pipeline | Rows affected | Fix |
|---|---:|---|
| SKU codes re-issued in an ERP migration, plus lowercase/whitespace variants | 13,459 | Normalised to canonical code |
| Sales logged in cases (`CS` / `cs` / `Case`) instead of units | 603 | Converted using case pack |
| Weekly POS file loaded twice | 10,453 | Kept first load |
| Store feed outage (2 days) | 2 store-days | Flagged `missing_data`, **not** zero |
| Christmas closures | 8 store-days | Recognised as a closure, not an error |
| Prices keyed in cents | 25 | ÷100, flagged |
| Missing price weeks | 18,472 | Forward-filled, flagged (99.2% exactly match truth) |
| SKUs with no case pack | 8 | Department mode, flagged |

**Verified, not asserted:** `pytest` checks the cleaned layer against a separate ground-truth database. Outside the two unrecoverable outage days, every one of the 3.65M daily sales rows matches truth exactly.

**Honest limits:**
- The data issues were injected deliberately, so this proves the pipeline handles these failure classes, not unknown ones.
- Case packs, costs, suppliers and inventory are synthetic.
- The agent's "today" is 2016-05-22, the end of the M5 data.

## Run it
```bash
pip install -e ".[dev]"            # or: uv sync
python scripts/fetch_m5.py         # ~325 MB from a public Hugging Face mirror of M5
python -m stockroom.data.pipeline  # ~20 s: ground truth -> messy warehouse -> cleaned layer
pytest -q                          # 9 tests
```

## Roadmap
Tools and a guarded SQL layer (P2) → forecasting and reorder drafting (P3) → agent loop (P4) → **120-question eval with bootstrap CIs, model comparison, raw-vs-clean ablation** (P5) → MCP server and Claude Code skill (P6) → web UI with a PO approval queue (P7). Details in [`SPEC.md`](SPEC.md) and [`docs/adr/`](docs/adr).

## Out of scope
Sending orders to suppliers, live ERP integration, auth/multi-tenancy, fine-tuning. The agent drafts; humans decide.

---
*Data: M5 Forecasting competition (Walmart / University of Nicosia), via a public Hugging Face mirror. Built with Claude Code; see [`CLAUDE.md`](CLAUDE.md).*
