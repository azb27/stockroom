# ADR 0005: Deterministic scoring with an ANSWER line; templated questions from ground truth

**Status:** accepted · **Phase:** P5 (supersedes the "LLM judge + human agreement" plan in SPEC §5)

## Context
The headline number of this project is agent accuracy. It must be reproducible, cheap to recompute, and hard to argue with. Free-text answers are hard to score. LLM judges add their own error, cost and drift, and validating one properly needs human labels we don't have at scale.

## Decision
- The harness appends one format instruction to every question: finish with `ANSWER: <value>`. Use `UNKNOWN` when missing data makes the value unknowable, and `CANNOT_ANSWER` for out-of-scope or declined requests.
- **Scorers are plain functions** (`evals/scoring.py`) with unit tests:
  - exact integers
  - relative or absolute numeric tolerance
  - SKU-set equality
  - labels
  - behavioural checks for traps: must say unknown and must not say zero; must decline and must not claim an action (negation-aware); must flag the outage; must flag imputed data; must state the 28-day limit
- **Questions are generated from templates** (`evals/build_questions.py`), with parameters sampled by a fixed seed and answers computed from `ground_truth.duckdb`. The generator is deterministic (tested by hashing two builds) and asserts eval-validity rules. Example rule: no non-trap question may cover the outage unless it expects known-days values with a flag.
- **Scoring is separate from running.** `evals/rescore.py` re-applies scorers to stored answers, so fixing a scorer never needs new API calls. Reworded questions are re-run.
- **Every post-hoc change is logged** in `evals/CORRECTIONS.md` with its reason and effect, and applied identically to all configurations.

## Consequences
- Numbers are reproducible from `runs/evals/*/results.jsonl` with no model in the loop.
- The format instruction slightly changes the task. It's the same for every configuration, and the answer text above the ANSWER line is untouched and shown in failure analyses.
- Deterministic trap scorers can be gamed by phrasing. They are checked against real answers, and each fix to one comes with a unit test built from the answer that exposed the bug.
- Templated questions test this agent on this data. They are not a general benchmark, and the README says so.

## Alternatives considered
- **LLM-as-judge:** flexible, but the judge's error sits inside the headline number, and calibrating it needs human labels. It's still useful later for grading explanation quality, as a secondary metric.
- **Hand-written questions only:** more natural phrasing, but expensive, and answers would be typed by hand rather than computed. Mixed approach for the future: add a hand-written set from a real customer backlog.
- **Structured output (a JSON answer tool):** the most robust to parse, but it changes the agent's action space during evaluation. The ANSWER line keeps the production loop untouched.
