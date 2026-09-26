# Week-2 plan: from demo to daily use at Larkspur

*Written at the end of week 1, for the ops lead and the buyer. It covers what we proved, what we didn't, and what we'd do next, in priority order.*

## Where we are
| Success criterion (discovery memo) | Status | Evidence |
|---|---|---|
| 1. ≥ 85% of ops questions answered correctly | **Met on our question set, not yet on yours** | 120/120 with Sonnet 5 (`docs/results/eval.md`). The questions are templated, built by us around the problems we found. They are not your analysts' real backlog. |
| 2. No missing day reported as zero | **Met** | 9 outage questions, 36 answers across 4 runs: each said UNKNOWN or flagged the total as partial, and none reported a zero |
| 3. Buyer approves or edits every PO | **Met by design** | No tool can approve or send. Approval is a named human action (CLI or UI). |
| 4. Forecast beats the same-weekday method | **Met at SKU level; roughly a tie on slow sellers** | WAPE 73.2% vs 76.8%, CI on the gain [+3.3, +4.0] pp; slow sellers 116.7% vs 115.9% (`docs/results/forecast_backtest.md`) |

The honest gap is criterion 1. A score on questions we wrote is evidence the system works. It is not yet evidence it answers *your* questions.

## Priorities for week 2
| # | Work | Why now | Owner | Done when |
|---|---|---|---|---|
| 1 | **Collect 50 real questions** from the analysts' inbox and last month's Excel requests. Label the answers together, and add them to the eval as a separate "backlog" set. | Turns criterion 1 from our claim into your measurement | Analysts (2 h) + FDE | Backlog accuracy reported with a CI next to the templated score |
| 2 | **Shadow-mode buyer pilot at one store (CA_2).** The agent drafts every morning; the buyer orders as usual. | We learn where the reorder policy differs from the buyer's judgement before it affects stock | Buyer + FDE | 10 days of drafts vs actual orders: lines matched, quantities within ±1 case, and every rejection reason logged |
| 3 | **Detect new kinds of dirty data, not just the known ones.** Today's cleaning rules catch the seven problems we found. Add load-level checks that flag anything new: row counts per file against history, the same file loaded twice under any name, new unit codes, prices outside the usual range. | The next migration or feed change won't look like the last one | FDE | Each check has a test with a planted example. New alerts appear in `core.dq_issues`. |
| 4 | **Put metric definitions in tools.** Add a zero-filled daily sales view, and send "rate" and "days of cover" questions to tools that compute them. | Closes the open failure pattern written up in `eval_failure_analysis.md` #2 (wrong denominator) | FDE | The failing question passes, and a new eval question guards it |
| 5 | **Rolling-origin forecast backtest** over 4 origins, plus a simple intermittent-demand method for slow sellers | One backtest month is one draw. Slow sellers are where the model doesn't beat your rule. | FDE | A WAPE gain with a CI across origins; the slow-seller method is kept only if it wins |
| 6 | **Read-only ERP connection** that replaces the CSV exports: nightly extract, a written data contract, and failure alerts | Removes the manual export step (P1) and makes freshness visible | Larkspur IT + FDE | A pipeline run from the extract matches the CSV run row for row |

## Decisions we need from Larkspur
- **Where it runs:** in your cloud account (recommended; data stays with you) or hosted by us.
- **Who may use it:** named analysts and the buyer only, or anyone in ops. This decides whether we add SSO in week 2 or week 3.
- **Reorder policy defaults:** service level (today 95%) and review period (today 7 days). These are business choices, not model choices.
- **Model spend:** a monthly cap for the Anthropic workspace. At the measured $0.021 per question, 50 questions a working day is about $23 a month on Sonnet. Haiku costs about half and scored 96%, with most misses on multi-step questions.

## Risks
| Risk | Mitigation |
|---|---|
| Real questions are harder than ours (the templated set is saturated at 100%) | That is why #1 comes first. Expect a lower number and report it. |
| The buyer ignores drafts in shadow mode | 10-minute daily review with the FDE for the first 3 days, and rejection reasons captured in one click |
| The ERP extract changes its schema without notice | The data contract plus load-level checks (#3). The pipeline fails loudly instead of cleaning silently. |
| Trust drops after one wrong answer | Every answer shows its tool calls and caveats. Wrong answers become eval questions (runbook), and the fix is shown. |

## Not in week 2
Sending orders to suppliers, price changes, and a multi-tenant product. None of these is blocked technically; each needs a decision from you and a separate approval path.
