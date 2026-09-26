# Runbook: operating Stockroom

*For whoever runs Stockroom after the forward-deployed engineer leaves: Larkspur's ops analyst or an on-call engineer. The commands are the ones in `CLAUDE.md`.*

## What runs where
| Piece | What it is | State it writes |
|---|---|---|
| `stockroom.data.pipeline` | ERP/POS exports → `raw.*` → cleaned `core.*` plus the data-quality log | `warehouse.duckdb` (rebuilt each run) |
| `stockroom.forecast.train` | Backtest, then the final LightGBM models and 28-day batch forecasts | `forecast.duckdb`, `models/` |
| `stockroom-web` | Chat UI and API, with the PO approval queue | `app.duckdb` (drafts, traces), `runs/traces/*.jsonl` |
| `stockroom-mcp` | The same tools for Claude Desktop / Claude Code | `runs/mcp/calls.jsonl` (audit) |
| `stockroom.approvals` | Human decisions on drafts (CLI) | `app.duckdb` |

The tools read `warehouse.duckdb` and `forecast.duckdb` read-only. **Only `app.duckdb` is ever written while the app runs**, and it is opened per operation, so the CLI and the web app can both write to it without locking each other out.

## Routine
| When | Do | Check |
|---|---|---|
| Each new export (daily) | `python -m stockroom.data.pipeline` (about 20 s) | Read `core.dq_issues`: counts should look like the last run's. A new issue type, or a jump in rows affected, means stop and investigate (below). |
| Weekly, after the Saturday–Friday week closes | `python -m stockroom.forecast.train` (about 15 min on 2 cores) | Open `docs/results/forecast_backtest.md`. The model must still beat the same-weekday baseline at SKU level, and "above P90" should stay near 10%. If not, keep last week's `forecast.duckdb`. |
| Before any change to prompts, tools, cleaning or model | `python -m evals.run --config sonnet_v2 --budget 5`, then `python -m evals.report` | Overall accuracy stays inside the last run's CI (`docs/results/eval.md`). No missing-day question may be answered with a number. |
| On every pull request | CI runs lint, the data build, tests, the web build and a 20-question Haiku smoke eval | Fails if the smoke eval drops more than 10 points below `evals/baseline.json`. |
| Daily, by the buyer | `python -m stockroom.approvals list --status PENDING_APPROVAL`, or the approval panel in the UI | Every draft is approved (with a name) or rejected (with a reason). Nothing is ever sent automatically. |

## When something goes wrong

**"The agent gave a wrong number."**
1. Find the turn: `SELECT * FROM traces WHERE question ILIKE '%…%' ORDER BY started_at DESC` in `app.duckdb`, or grep `runs/traces/`. Each turn stores every tool call, its SQL, the result and the caveats.
2. Decide whose error it is:
   - **Data:** the SQL was right but `core.*` is wrong. Fix it in the cleaning layer (below).
   - **Reasoning:** the SQL was wrong, as with a wrong denominator or a case-sensitive filter. See `docs/results/eval_failure_analysis.md` for known patterns.
   - **Question:** the question was ambiguous. The answer should have stated its assumption.
3. **Add the question to the eval** (`evals/build_questions.py`) with its answer from ground truth, so the fix is measured and can't silently regress.
4. If the metric has one correct definition, put it in a tool rather than in the prompt. That is the standing lesson from failure #2.

**The data-quality log shows something new.**
1. Don't patch the data by hand. Write the rule in `src/stockroom/data/sql/core.sql`, detecting the problem from `raw.*` alone (CLAUDE.md rule 2), and log it to `core.dq_issues`.
2. Add a test to `tests/test_data_layer.py` that proves the cleaned layer recovers the truth.
3. Re-run the pipeline, the tests and the eval. Tell the analysts what changed and which past numbers moved.

**A store's feed is missing for a day.** No action is needed for correctness. The day is marked `missing_data`, every tool caveats it, and the agent reports it as unknown. Ask the store to resend. The next pipeline run fills it in.

**The demo's chat says the budget is used up.** This is working as intended. The daily cap resets at 00:00 UTC. To change it, set `STOCKROOM_DAILY_BUDGET_USD` (plus `STOCKROOM_QUESTIONS_PER_HOUR`, `STOCKROOM_CONV_BUDGET_USD` and `STOCKROOM_MAX_CONCURRENT`) and restart. The workspace spend limit in the Anthropic console is the backstop, and it should always sit below what you're willing to lose.

**The model API is down or slow.** The UI shows "The model call failed (…). Please retry.", and the turn costs only what it spent. There's nothing to repair. The data, the forecasts and the approval queue all still work without the model.

**`Could not set lock on file app.duckdb`.** Something is holding a connection open. Every write goes through `appdb.app_session()`, which retries for up to 10 s. A lasting lock means someone opened the file in a DuckDB shell and left it open, so close it.

## Keys and access
- **API key:** use a separate Anthropic workspace per environment (dev, demo, production), each with a monthly spend limit. Keys live in untracked env files or the host's secret store, never in the repo; the pre-commit hook blocks `sk-ant-` strings.
- **Rotate a key:** create the new key, update the secret, restart the service, confirm with one question, then revoke the old key.
- **`ground_truth.duckdb` is the eval's answer key.** It never goes on a server, into an image or near the tools.

## Who to call
| Situation | Owner |
|---|---|
| A wrong answer, or a new data-quality pattern | FDE on the engagement, then Larkspur's ops analyst for business meaning |
| A draft PO looks wrong | The buyer. Reject it with a reason; reasons are the feedback loop for the reorder policy. |
| Spend or access | Whoever owns the Anthropic workspace |
