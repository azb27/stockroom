# ADR 0003: Eval answers are computed from ground truth, not from the cleaned layer

**Status:** accepted · **Phase:** P5

## Context
If eval answers were computed from `core.*`, a bug in the cleaning layer would be invisible: the agent would be "right" about wrong data.

## Decision
- Every eval question's reference answer is computed by SQL against `ground_truth.duckdb`.
- The agent only ever sees `warehouse.duckdb`.
- Questions touching unrecoverable data (e.g. CA_4 on 2016-03-14/15) expect the agent to report the data as missing. They are scored by rubric, not by the true value.

## Consequences
- The eval measures end-to-end correctness as the customer experiences it (cleaning + tools + model).
- It enables the raw-vs-core ablation: the same questions with the agent pointed at `raw.*` quantify what the cleaning layer is worth.
- Reference answers are code, so they are reviewable and re-runnable. No hand-typed numbers.
