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
| `forecast_demand(sku, store=None, horizon_days=14)` | SKU (alias codes accepted), store or all, horizon ≤28 | daily mean with P10/P90, horizon total, recent actuals, model version, category backtest WAPE vs baseline | Rejects horizon >28, unknown SKU (suggests close matches) |
| `detect_anomalies(scope='all', store, dept, end_date, limit=20)` | `sales` / `inventory` / `data_quality` / `all` | counts, $ impact, ranked list per type | Max 50 results; inventory only on the as-of date; no future dates |
| `explain_variance(period_a_start, period_a_end, period_b_start, period_b_end, by='dept', store)` | two date ranges; group by dept / category / store / sku | revenue delta decomposed into volume / price / mix effects, per group | Periods must be inside data; flags periods overlapping missing store-days |
| `draft_reorder(store, dept=None, skus=None, review_days=7, service_level=0.95)` | scope + policy knobs | draft PO lines: SKU, on hand, on order, forecast over lead time + review period, safety stock, order qty (cases), est. cost | Writes only to `po_drafts` in `app.duckdb` (a separate file) with status `PENDING_APPROVAL`. **No tool can approve or send.** |

Reorder policy (periodic review, order-up-to; see `tools/reorder.py` and ADR 0004):
`need = Σ mean(next L+R days) + z·√Σσ_d² − on_hand − on_order`, with σ_d = (P90 − mean)/1.2816 (upper tail only).
- Order `ceil(need / case_pack)` whole cases when need > 0.
- One draft per supplier. Supplier minimums are flagged, never auto-inflated.
- Defaults: R = 7 days, service level 95% (z = 1.645).

**Approval** happens only in the UI or `stockroom approve <id>` CLI. It is a human action, logged with who and when.

## 4. Agent loop

- Anthropic Python SDK, manual loop (`agent/loop.py`). Models: `claude-sonnet-5` (default), `claude-haiku-4-5-20251001` (cheap tier), set via env.
- Sonnet 5 uses adaptive thinking. `STOCKROOM_EFFORT` (default `medium`) sets reasoning depth. Thinking blocks are passed back unchanged each round.
- System prompt: role, as-of date, "always surface caveats from tool results", "never state a number you did not get from a tool", "you cannot approve or send POs", and out-of-scope list.
- Limits: 12 tool calls per turn, $0.50 per conversation (computed from `usage`), 60s wall clock.
- Tracing: every turn is written to `traces` in `app.duckdb` (JSONL mirror in `runs/`): messages, tool inputs and outputs, tokens, cost, latency. The UI renders the tool trace; evals read it.
- Automatic prompt caching (`cache_control` at the request level). The system prompt, tools and growing conversation are re-read from cache each round.

## 5. Evaluation (the headline)

`evals/questions.jsonl`: 120 questions, 30 per tier, built by `python -m evals.build_questions` (fixed seed).

| Tier | What it tests | Example | Scoring |
|---|---|---|---|
| T1 Lookup | single fact | "Units of FOODS_3_090 sold at CA_1 on 2016-05-01?" | exact int |
| T2 Aggregation | group/filter/time | "Top 5 household SKUs by revenue in April 2016 at CA_3" | set match / ±0.5% |
| T3 Multi-step | chained tools + judgement | "Which CA_2 FOODS_3 SKUs will stock out within their supplier lead time?" | set F1 vs truth |
| T4 Traps | unanswerable or dirty | "Sales at CA_4 on 2016-03-14?" (must say missing, not 0); "Texas sales?"; "Send the PO to the supplier"; alias code queries; Christmas | deterministic behaviour checks on the `ANSWER:` line (ADR 0005; no LLM judge) |

- **Answers are computed from `ground_truth.duckdb`, not `core.*`.** Cleaning mistakes count against the agent, as they would for the customer.
- Each question: `id, tier, kind, question, expected, scorer, tol`. The expected value is computed when the set is built.
- **Report:** accuracy per tier and overall with **95% bootstrap CIs** (10k resamples); paired comparison between configs with McNemar's test; p50/p95 latency; mean $/question; mean tool calls.
- **Configs compared:**
  1. Sonnet
  2. Haiku
  3. Router (Haiku first; escalate to Sonnet if Haiku gives no ANSWER line, hits a cap or gets a tool error). Replayed offline from runs 1 and 2.
  4. **Ablation: Sonnet on `raw.*` instead of `core.*`**
- Config 4 answers "was the cleaning layer worth it?" with a number. That chart is the FDE story.
- **CI** (GitHub Actions): on PRs touching `src/`, run a 20-question smoke set on Haiku (~$0.10) and fail if accuracy is more than 10pp below `evals/baseline.json`. Full run via `workflow_dispatch`.
- With n=30 per tier, CIs are wide (~±15pp). Say so. Don't over-read tier differences.

## 6. Forecasting

As built (ADR 0004):
- **Model:** one global LightGBM across all store-SKU series, **direct** rather than recursive. Every demand feature looks back ≥ 28 days, so one model covers the 28-day horizon.
- **Features:**
  - lags 28/35/42/49
  - rolling mean/std/zero-share over windows ending 28 days back
  - price, price vs. 8-week mean, week-on-week price change
  - SNAP, event type, day of week/month, series age
  - store/dept/category/SKU as categoricals
- **Mean** from a Tweedie model. **P10/P90** from quantile models trained on demand relative to the series' recent level.
- **Backtest:** origin 2016-04-24. Early stopping on the 28 days before the origin; refit to the origin; score the next 28 days with prices frozen at the origin. A test proves the features don't change when all post-origin data is deleted.
- **Report:** WAPE and bias vs. the customer's method (same weekday, last 4 weeks), plus a bootstrap CI on the improvement and one-sided quantile calibration.
  - Covered overall, by category, store and velocity, and at store-dept level.
  - Segments where the model loses are named in the generated doc.
- **Serving:** batch-scored nightly into `forecast.duckdb`; the tool reads rows, it doesn't run the model. Training is deterministic (fixed row order + seed).
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
- [x] **P3: Forecast + reorder.**
  - Direct LightGBM (Tweedie mean + scaled quantile P10/P90), leak-proof backtest, batch scoring into `forecast.duckdb`.
  - Tools: `forecast_demand`, `draft_reorder`. Drafts go to `app.duckdb`; human-only `stockroom.approvals`. ADR 0004.
  - *Done:* 92 tests pass.
    - Store-SKU-day WAPE 73.2% vs baseline 76.8% (+3.6 pp, 95% CI [+3.3, +4.0]); store-dept-day 9.2% vs 9.9%. Loses only on slow sellers (116.7% vs 115.9%).
    - P90 exceeded on 11.7% of days (target 10%).
    - No-look-ahead test, with a mutation check proving it can fail.
    - Hand-checked reorder case; deterministic training.
- [x] **P4: Agent loop + CLI.**
  - `python -m stockroom.agent`: a plain Messages API loop with automatic prompt caching and configurable effort (adaptive thinking).
  - Caps: 12 tool calls per turn, $0.50 per conversation, 60 s per turn. Traces go to `app.duckdb` and `runs/*.jsonl`.
  - *Done:* 11 offline tests with a scripted fake model (caps, parallel calls, error recovery, caveats surviving truncation).
    - Live session: **5/5 correct** (`docs/results/agent_session_p4.md`), including an exact count across an ERP code change, revenue to the cent, "unknown, not zero" for the outage day, and refusing to send a PO.
    - Total cost $0.10.
  - Lesson for P5: in one conversation, later questions reuse caveats from earlier ones. Every eval question gets a fresh agent.
- [x] **P5: Evals (3 days). ← the headline.**
  - 120 questions, harness, all 4 configs, `docs/results/eval.md` with CI plot, CI workflow.
  - *Done:* results in `docs/results/eval.md` (generated by `python -m evals.report`):
    - Sonnet 5 **120/120**, $0.021/question, median 6.3 s.
    - Haiku 4.5 **96% [92, 99]**, $0.012/question.
    - Router: 96%. It escalated 4 questions and gained nothing (Sonnet vs Haiku 5–0, p = 0.06).
  - Cleaning layer, same model: **100% vs 60%** on lookups and aggregations (T1 + T2), 63% overall on raw data (McNemar 44–0 over all 120, p = 1e-13).
  - Missing days: 9 questions touch the outage, and none was answered as zero in any cleaned run. Both success criteria are met.
  - The eval found a product bug: a case-mismatched filter returned empty and was reported as 0. `run_sql` now names the mismatch. Haiku went from 92% to 96% (T3 67% → 87%), p = 0.18.
  - Failure write-up: `docs/results/eval_failure_analysis.md` covers 4 traced failures. `evals/CORRECTIONS.md` lists 6 fixes to the eval itself.
  - Deterministic scoring: ADR 0005. There are 118 tests.
  - CI runs lint, the data build and tests on every push, plus a 20-question Haiku smoke eval on PRs against `evals/baseline.json`.
  - Total API spend about $12.
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
