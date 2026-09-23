# Stockroom: build spec

> An ops agent for a regional distributor that answers questions over messy ERP data, forecasts demand, flags anomalies, and drafts purchase orders a human must approve. It is evaluated on 120 questions with ground truth and bootstrap confidence intervals.

**Thesis this project proves:** *I build agents that make decisions on messy numbers, and I can prove statistically when they're wrong.*

**Target reader:** a hiring manager for FDE / Applied AI / AI Engineer roles who spends 90 seconds on the README and 5 minutes on the live demo.

---

## 1. The engagement (the story the repo tells)

Larkspur Distribution (fictional) supplies 4 retail accounts in California with ~3,000 SKUs across food, household and hobby categories. Their ops team answers every question ("what sold last week?", "what do we need to reorder?", "why is revenue down?") by exporting from the ERP into Excel. Their data has the usual scars of a real business: a half-finished ERP migration, a store that logs some items in cases, a POS file loaded twice, an outage, fat-fingered prices.

The brief is in `docs/engagement/discovery-memo.md`. Every tool below traces back to a pain point in that memo. **That traceability is the FDE signal.**

Data honesty (README must say this plainly):
- Sales, prices and calendar are real: M5 (Walmart, CA stores, 2014-05-24 → 2016-05-22).
- Case packs, costs, suppliers, lead times and inventory are synthetic (seeded, `truth.py`).
- Data-quality issues are injected deliberately (`dirt.py`) so cleaning can be tested against known truth. This proves the pipeline handles these failure classes; it does not prove it catches unknown ones.
- The agent's "today" is 2016-05-22.

## 2. Architecture

```
                         ┌────────────────────────── evals/ ──────────────────────────┐
                         │ 120 questions · answers computed from ground_truth.duckdb  │
                         │ bootstrap CIs · per-tier accuracy · $/q · p50/p95 latency  │
                         └──────────────────────────────┬─────────────────────────────┘
                                                        │ drives
  Next.js UI (Vercel) ──HTTP/SSE──> FastAPI (Fly/Render) ──> agent loop (Anthropic Client SDK)
   chat · tool trace ·                                        │  max 12 tool calls · $ cap · tracing
   PO approval queue                                          │
                                                              ▼
                                         stockroom.tools  (pure Python, typed, unit-tested)
                                         ├── describe_data   ├── forecast_demand
                                         ├── run_sql (guarded)├── detect_anomalies
                                         ├── explain_variance └── draft_reorder ──> po_drafts (PENDING)
                                                              │
                              ┌───────────────────────────────┼───────────────────────┐
                              ▼                               ▼                       ▼
                     warehouse.duckdb                 MCP server (FastMCP)    Claude Code skill
                     raw.*  (customer's mess)         same tools, for Claude  .claude/skills/ +
                     core.* (cleaned, READ-ONLY)      Desktop / Code / any     .mcp.json
                     app.duckdb (drafts, traces;      MCP client
                       separate file, writable)

       ground_truth.duckdb ── used ONLY by tests/ and evals/. Never reachable from tools.
```

Why this shape: see `docs/adr/`. In short:
- **One tool library, two front doors.** The MCP server makes the tools portable (it's the deliverable Anthropic's FDE postings name). The web agent calls the same functions in-process for full control of approvals, cost and tracing.
- **Client SDK, not Agent SDK, for the web agent.** The Agent SDK runs the Claude Code binary and ships file/shell tools we'd have to lock down. It is revisited in Phase 9 for an autonomous overnight job, where its sessions and hooks earn their weight.

## 3. Tool contracts

All tools return JSON-serialisable dicts with `data`, `caveats` (list of strings, e.g. "CA_4 has missing data on 2016-03-14") and `provenance` (tables/rows touched). **Caveats are how data-quality knowledge reaches the model.**

| Tool | Input | Output | Guardrails |
|---|---|---|---|
| `describe_data()` | none | schemas of `core.*`, as-of date, `core.dq_issues`, store-day status summary | n/a |
| `run_sql(query)` | SQL string | ≤200 rows + column types + truncated flag | Read-only connection. Parsed with `sqlglot`: single SELECT/WITH only; tables must be `core.*`; 5s timeout; no `raw.*`, no `app.*` |
| `forecast_demand(sku, store=None, horizon_days=14)` | SKU (alias codes accepted), store or all, horizon ≤28 | daily P10/P50/P90, model version, segment backtest WAPE | Rejects horizon >28, unknown SKU (suggests close matches) |
| `detect_anomalies(scope='all', store, dept, end_date, limit=20)` | `sales` / `inventory` / `data_quality` / `all` | counts, $ impact, ranked list per type | Max 50 results; inventory only on the as-of date; no future dates |
| `explain_variance(period_a_start, period_a_end, period_b_start, period_b_end, by='dept', store)` | two date ranges; group by dept / category / store / sku | revenue delta decomposed into volume / price / mix effects, per group | Periods must be inside data; flags periods overlapping missing store-days |
| `draft_reorder(store, dept=None, skus=None)` | scope | draft PO lines: SKU, on hand, on order, forecast over lead time + review period, safety stock, order qty (cases), est. cost | Writes only to `po_drafts` in `app.duckdb` (a separate file) with status `PENDING_APPROVAL`. **No tool can approve or send.** |

Reorder policy (document in code and README):
`order_units = max(0, forecast(L + R) + z·σ_daily·√(L + R) − on_hand − on_order)`, then round up to whole cases and at least `min_order_cases`. Defaults: R = 7 days, z = 1.65 (95% cycle service).

**Approval** happens only in the UI or `stockroom approve <id>` CLI. It is a human action, logged with who and when.

## 4. Agent loop

- Anthropic Python SDK, manual loop (readable in one file, ~150 lines). Models: `claude-sonnet-5` (default), `claude-haiku-4-5-20251001` (cheap tier), set via env.
- System prompt: role, as-of date, "always surface caveats from tool results", "never state a number you did not get from a tool", "you cannot approve or send POs", and out-of-scope list.
- Limits: 12 tool calls per turn, $0.50 per conversation (computed from `usage`), 60s wall clock.
- Tracing: every turn is written to `traces` in `app.duckdb` (JSONL mirror in `runs/`): messages, tool inputs and outputs, tokens, cost, latency. The UI renders the tool trace; evals read it.
- Prompt caching on the system prompt and tool definitions.

## 5. Evaluation (the headline)

`evals/questions.yaml`: 120 questions, 30 per tier.

| Tier | What it tests | Example | Scoring |
|---|---|---|---|
| T1 Lookup | single fact | "Units of FOODS_3_090 sold at CA_1 on 2016-05-01?" | exact int |
| T2 Aggregation | group/filter/time | "Top 5 household SKUs by revenue in April 2016 at CA_3" | set match / ±0.5% |
| T3 Multi-step | chained tools + judgement | "Which CA_2 FOODS_3 SKUs will stock out within their supplier lead time?" | set F1 vs truth |
| T4 Traps | unanswerable or dirty | "Sales at CA_4 on 2016-03-14?" (must say missing, not 0); "Texas sales?"; "Send the PO to the supplier"; alias code queries; Christmas | rubric: LLM judge + 30-item human-agreement check |

- **Answers are computed from `ground_truth.duckdb`, not `core.*`.** Cleaning mistakes count against the agent, as they would for the customer.
- Each question: `id, tier, question, answer_sql | answer_fn, scorer, tolerance, tags`.
- **Report:** accuracy per tier and overall with **95% bootstrap CIs** (10k resamples); paired comparison between configs with McNemar's test; p50/p95 latency; mean $/question; mean tool calls.
- **Configs compared:**
  1. Sonnet
  2. Haiku
  3. Router (Haiku first, escalate to Sonnet on low confidence or tool error)
  4. **Ablation: Sonnet on `raw.*` instead of `core.*`**
- Config 4 answers "was the cleaning layer worth it?" with a number. That chart is the FDE story.
- **CI** (GitHub Actions): on PRs touching `src/`, run a 20-question smoke set on Haiku (~$0.10) and fail if accuracy is more than 10pp below `evals/baseline.json`. Full run via `workflow_dispatch`.
- With n=30 per tier, CIs are wide (~±15pp). Say so. Don't over-read tier differences.

## 6. Forecasting

- One global LightGBM model (Tweedie objective) across all store-SKU series.
- **Features:** lags 7/14/28, rolling means and std, price, price vs. 4-week mean, SNAP, events, day of week, store/dept/category.
- **Quantiles:** P10 and P90 from separate quantile-objective models.
- **Backtest:** train through 2016-04-24, test on 2016-04-25 → 2016-05-22 (28 days), using the `core.*` data.
- **Report:** WAPE and pinball loss vs. a seasonal-naive baseline (same weekday, last 4 weeks), overall and by category. If LightGBM doesn't beat the baseline in a category, say so.
- **Serving:** retrain on everything and store as `models/lgbm_{version}.txt`. The tool loads it once and forecasts recursively. The model file size must stay reasonable for the deploy image.
- **Do not** compare against or claim M5 leaderboard results. Different subset, different metric.

## 7. Phases

Each phase ends with passing tests and a commit. Estimates assume ~3 focused hours/day.

- [x] **P1: Data layer.**
  - Ground truth, raw warehouse with 7 injected issue classes, cleaned `core.*`, `core.dq_issues`.
  - *Done:* `make data && make test` passes 9/9; sales recovered exactly; price imputation 99.2% exact.
- [x] **P2: Tools library.**
  - `src/stockroom/tools/`: `describe_data`, `run_sql`, `detect_anomalies`, `explain_variance`, plus a registry with strict JSON schemas and a single `call()` entry point.
  - Two independent SQL safety layers:
    1. a sqlglot allowlist
    2. a read-only DuckDB connection with external access disabled and settings locked
  - *Done:* 76 tests pass.
    - 27 attacks rejected by the guard alone; 7 blocked by the connection alone.
    - Tool revenue within 0.0002% of truth.
    - All 498 planted stock-outs found; 99.7% of overstock flags are correct.
    - Volume + mix + price sums to the delta for every group.
- [ ] **P3: Forecast + reorder (2 days).**
  - Training script, backtest table in `docs/results/forecast_backtest.md`, `forecast_demand`, `draft_reorder`, `po_drafts` in `app.duckdb`.
  - *Done:* backtest table exists; the reorder math has a hand-checked test case.
- [ ] **P4: Agent loop + CLI (2 days).**
  - `stockroom chat` in the terminal, tracing, cost cap, caveat surfacing.
  - *Done:* 5 hand-picked questions answered correctly in a recorded session.
- [ ] **P5: Evals (3 days). ← the headline.**
  - 120 questions, harness, all 4 configs, `docs/results/eval.md` with CI plot, CI workflow.
  - *Done:* results table plus a written "where it fails" section with 3 real failure traces.
- [ ] **P6: MCP server + Claude Code skill (1 day).**
  - `stockroom-mcp` (FastMCP, stdio + streamable HTTP), `.mcp.json`, `.claude/skills/stockroom-analyst/SKILL.md`.
  - *Done:* works from Claude Desktop and Claude Code; screenshot in README.
- [ ] **P7: API + UI + deploy (3 days).**
  - FastAPI with SSE, Next.js chat with tool-trace panel and PO approval queue.
  - Backend on Fly.io or Render (DuckDB baked into the image); frontend on Vercel.
  - *Done:* live URL.
- [ ] **P8: Ship (1 day).**
  - README as a product spec: problem → demo GIF → architecture → eval table → cost → limitations.
  - Engagement docs complete: runbook and week-2 plan.
  - 90-second Loom and a LinkedIn post with one real number.
- [ ] **P9 (stretch).**
  - Overnight replenishment job on the Claude Agent SDK: sessions and hooks, with a `PreToolUse` hook enforcing the approval policy.

Why evals (P5) come before UI (P7): the UI is the demo, the eval table is the proof. If time runs out, a CLI plus an eval table beats a pretty UI with no numbers.

## 8. Out of scope (say it in the README)

- Real ERP/POS integration, auth, multi-tenancy.
- Sending anything to suppliers. The agent drafts; humans decide.
- Real-time data, streaming.
- Fine-tuning.
- Price optimisation or promo planning.
- Generalising the cleaning rules to unseen data-quality problems. They're tested against injected, known issues.

## 9. Carry-over to Skeptic (project 2)

Build these generically so they can be lifted out:
- `evals/stats.py`: bootstrap CI, McNemar, paired bootstrap on differences.
- The trace/cost accounting module.
- The MCP server pattern and guard tests.

**Skeptic needs from Aziz:** the XAUUSD pipeline repo (or its feature/label code), the data source and date range, and how the current 89% directional accuracy is measured.
