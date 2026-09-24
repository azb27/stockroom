"""Build docs/results/eval.md and eval_accuracy.png from the run results.

    python -m evals.report

Reads runs/evals/{sonnet,haiku,sonnet_raw}/results.jsonl. The router (Haiku first, escalate to
Sonnet) is replayed from the two single-model runs: its escalation rule looks only at Haiku's own
output, so replaying is equivalent to running it live, and costs nothing extra.
"""

from __future__ import annotations

import datetime as dt
import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from stockroom import config
from stockroom.stats import mcnemar_exact

RUNS = config.RUNS_DIR / "evals"
OUT_MD = config.ROOT / "docs" / "results" / "eval.md"
OUT_PNG = config.ROOT / "docs" / "results" / "eval_accuracy.png"
TIERS = ["T1", "T2", "T3", "T4"]
TIER_NAMES = {"T1": "T1 Lookup", "T2": "T2 Aggregation", "T3": "T3 Multi-step", "T4": "T4 Traps"}
LABELS = {
    "sonnet": "Sonnet 5",
    "haiku": "Haiku 4.5",
    "router": "Router (Haiku → Sonnet)",
    "sonnet_raw": "Sonnet 5, raw data (no cleaning layer)",
}
BEFORE = {"sonnet": "sonnet", "haiku": "haiku"}  # run dirs of the first run (no empty-result hint)
AFTER = {"sonnet": "sonnet_v2", "haiku": "haiku_v2"}  # same agents with the hint
# dataviz reference palette, slots 1-3 (validated all-pairs) + neutral gray for the ablation baseline
COLORS = {"sonnet": "#2a78d6", "haiku": "#eb6834", "router": "#1baf7a", "sonnet_raw": "#8a8984"}
MARKERS = {"sonnet": "o", "haiku": "s", "router": "D", "sonnet_raw": "^"}


def load(name: str) -> dict[str, dict]:
    path = RUNS / name / "results.jsonl"
    if not path.exists():
        return {}
    return {r["id"]: r for r in (json.loads(x) for x in path.read_text().splitlines() if x.strip())}


def escalate(r: dict) -> bool:
    return r["error"] or r["answer_line"] is None or bool(r["limits_hit"]) or r["tool_errors"] > 0


def router(haiku: dict[str, dict], sonnet: dict[str, dict]) -> dict[str, dict]:
    out = {}
    for qid, h in haiku.items():
        if qid not in sonnet:
            continue
        if escalate(h):
            s = sonnet[qid]
            out[qid] = {**s, "cost_usd": h["cost_usd"] + s["cost_usd"], "latency_s": h["latency_s"] + s["latency_s"],
                        "tool_calls": h["tool_calls"] + s["tool_calls"], "escalated": True}  # fmt: skip
        else:
            out[qid] = {**h, "escalated": False}
    return out


def acc_ci(
    rows: list[dict], strata: bool, n_boot: int = 10_000, seed: int = 27
) -> tuple[float, float, float]:
    """Accuracy with a 95% percentile bootstrap CI. Overall resamples within each tier (fixed 30/tier design)."""
    rng = np.random.default_rng(seed)
    y = np.array([r["correct"] for r in rows], dtype=float)
    if not strata:
        idx = rng.integers(0, len(y), (n_boot, len(y)))
        draws = y[idx].mean(axis=1)
    else:
        groups = [np.array([r["correct"] for r in rows if r["tier"] == t], dtype=float) for t in TIERS]
        draws = np.zeros(n_boot)
        for g in groups:
            if len(g):
                draws += g[rng.integers(0, len(g), (n_boot, len(g)))].sum(axis=1)
        draws /= len(y)
    lo, hi = np.quantile(draws, [0.025, 0.975])
    return float(y.mean()), float(lo), float(hi)


def summarize(res: dict[str, dict]) -> dict:
    rows = list(res.values())
    s = {"n": len(rows), "overall": acc_ci(rows, strata=True)}
    for t in TIERS:
        tr = [r for r in rows if r["tier"] == t]
        s[t] = acc_ci(tr, strata=False) if tr else (float("nan"),) * 3
    data_q = [r for r in rows if r["tier"] in ("T1", "T2")]
    s["T1+T2"] = acc_ci(data_q, strata=False) if data_q else (float("nan"),) * 3
    lat = np.array([r["latency_s"] for r in rows])
    s["cost_total"] = sum(r["cost_usd"] for r in rows)
    s["cost_per_q"] = s["cost_total"] / len(rows)
    s["p50"], s["p95"] = float(np.percentile(lat, 50)), float(np.percentile(lat, 95))
    s["tools"] = float(np.mean([r["tool_calls"] for r in rows]))
    s["errors"] = sum(r["error"] for r in rows)
    s["limits"] = sum(bool(r["limits_hit"]) for r in rows)
    s["escalated"] = sum(r.get("escalated", False) for r in rows)
    return s


def paired(a: dict[str, dict], b: dict[str, dict]) -> tuple[int, int, float]:
    common = sorted(set(a) & set(b))
    b_only = sum(1 for q in common if not a[q]["correct"] and b[q]["correct"])
    a_only = sum(1 for q in common if a[q]["correct"] and not b[q]["correct"])
    return a_only, b_only, mcnemar_exact(a_only, b_only)


def chart(summ: dict[str, dict]) -> None:
    names = [n for n in ["sonnet", "haiku", "router", "sonnet_raw"] if n in summ]
    rows = ["overall", *TIERS]
    ylab = ["Overall (120)", *[TIER_NAMES[t] + " (30)" for t in TIERS]]
    fig, ax = plt.subplots(figsize=(8.6, 5.2), dpi=160)
    fig.patch.set_facecolor("#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    offs = np.linspace(0.27, -0.27, len(names))  # first series on top, matching legend order
    for i, n in enumerate(names):
        for j, key in enumerate(rows):
            p, lo, hi = summ[n][key]
            yy = len(rows) - 1 - j + offs[i]
            ax.plot([lo * 100, hi * 100], [yy, yy], color=COLORS[n], lw=2, solid_capstyle="round", zorder=2)
            ax.scatter([p * 100], [yy], s=46, marker=MARKERS[n], color=COLORS[n], edgecolor="#fcfcfb",
                       linewidth=2, zorder=3, label=LABELS[n] if j == 0 else None)  # fmt: skip
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(ylab[::-1], color="#0b0b0b", fontsize=9)
    ax.set_xlim(-2, 102)
    ax.set_xlabel(
        "Questions answered correctly (%), with 95% bootstrap interval", color="#52514e", fontsize=9
    )
    ax.grid(axis="x", color="#e4e3df", lw=0.8)
    ax.set_axisbelow(True)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(colors="#52514e", labelsize=8, length=0)
    ax.axhline(len(rows) - 1 - 0.5, color="#c9c8c3", lw=0.8)
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.45, -0.14),
        ncol=2,
        fontsize=8,
        frameon=False,
        labelcolor="#0b0b0b",
    )
    ax.set_title("Stockroom agent: 120 ground-truth questions", loc="left", fontsize=11, color="#0b0b0b")
    fig.tight_layout()
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PNG, facecolor=fig.get_facecolor())
    plt.close(fig)


def pct(t: tuple[float, float, float]) -> str:
    p, lo, hi = t
    return "n/a" if np.isnan(p) else f"**{p:.0%}** [{lo:.0%}, {hi:.0%}]"


def main() -> None:
    raw = {n: load(n) for n in ["sonnet", "haiku", "sonnet_raw", "sonnet_v2", "haiku_v2"]}
    before = {k: raw[v] for k, v in BEFORE.items() if raw[v]}
    after = {k: raw[v] for k, v in AFTER.items() if raw[v]}
    final = after if len(after) == 2 else before  # the current system, if its run exists
    res = dict(final)
    if "sonnet" in res and "haiku" in res:
        res["router"] = router(res["haiku"], res["sonnet"])
    if raw["sonnet_raw"]:
        res["sonnet_raw"] = raw["sonnet_raw"]
    order = [n for n in ["sonnet", "haiku", "router", "sonnet_raw"] if n in res]
    summ = {n: summarize(res[n]) for n in order}
    chart(summ)
    version = "with the empty-result hint" if final is after else "first run"

    L = [
        "# Evaluation: 120 ground-truth questions",
        "",
        (
            "Generated by `python -m evals.report` from `runs/evals/*/results.jsonl` "
            "(produced by `python -m evals.run --config <name>`). Do not edit by hand."
        ),
        "",
        (
            f"*Report built {dt.datetime.now():%Y-%m-%d %H:%M}. Question set: `evals/questions.jsonl` "
            "(built by `python -m evals.build_questions`, fixed seed).*"
        ),
        "",
        "![accuracy by tier](eval_accuracy.png)",
        "",
        "## Accuracy",
        (
            f"Current system ({version}). Share of questions answered correctly, with a 95% bootstrap interval (10,000 resamples; overall "
            "resamples within each tier). **With 30 questions per tier, tier intervals are wide (~±15 pp); don't "
            "over-read small differences between tiers.**"
        ),
        "",
        "| Configuration | Overall | T1 Lookup | T2 Aggregation | T3 Multi-step | T4 Traps |",
        "|---|---|---|---|---|---|",
    ]
    for n in order:
        s = summ[n]
        L.append(
            f"| {LABELS[n]} | {pct(s['overall'])} | {pct(s['T1'])} | {pct(s['T2'])} | {pct(s['T3'])} | {pct(s['T4'])} |"
        )
    L += [
        "",
        "## Cost and speed",
        "",
        "| Configuration | $ / question | Total $ | Median latency | p95 latency | Tool calls / q | Hit a limit | API errors |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for n in order:
        s = summ[n]
        L.append(f"| {LABELS[n]} | ${s['cost_per_q']:.4f} | ${s['cost_total']:.2f} | {s['p50']:.1f}s | {s['p95']:.1f}s | "
                 f"{s['tools']:.1f} | {s['limits']} | {s['errors']} |")  # fmt: skip
    if "router" in summ:
        L += ["", (f"Router rule: start with Haiku; escalate to Sonnet if Haiku gave no ANSWER line, hit a limit, or had a "
              f"tool error. It escalated **{summ['router']['escalated']} of {summ['router']['n']}** questions.")]  # fmt: skip

    L += ["", "## Are the differences real?", ("Paired comparison on the same questions (exact McNemar test). "
          "\"Only A right\" counts questions A got right and B got wrong."), "",
          "| A vs B | Only A right | Only B right | p-value |", "|---|---:|---:|---:|"]  # fmt: skip
    for a, b in [("sonnet", "haiku"), ("sonnet", "router"), ("sonnet", "sonnet_raw")]:
        if a in res and b in res:
            x, y, p = paired(res[a], res[b])
            L.append(f"| {LABELS[a]} vs {LABELS[b]} | {x} | {y} | {p:.3g} |")

    if len(after) == 2 and len(before) == 2:
        L += ["", "## Before and after the empty-result hint",
              ("The first run found agents filtering `status = 'active'` (the data says `'ACTIVE'`), getting an empty "
              "result, and answering 0. `run_sql` now names the mismatch when a result is empty. Same questions, same "
              "models, hint off vs on:"), "",
              "| Model | Overall before | Overall after | T3 before | T3 after | Fixed | Broke | McNemar p |",
              "|---|---|---|---|---|---:|---:|---:|"]  # fmt: skip
        for m in ("sonnet", "haiku"):
            sb, sa = summarize(before[m]), summarize(after[m])
            broke, fixed, p = paired(before[m], after[m])
            L.append(f"| {LABELS[m]} | {pct(sb['overall'])} | {pct(sa['overall'])} | {pct(sb['T3'])} | {pct(sa['T3'])} | "
                     f"{fixed} | {broke} | {p:.3g} |")  # fmt: skip
        L += [
            "",
            (
                '"Fixed" = wrong before and right after; "Broke" = the reverse. Model sampling also varies '
                "between runs, so some flips in either direction are noise; the McNemar test accounts for that."
            ),
        ]

    if "sonnet_raw" in summ and "sonnet" in summ:
        s, r = summ["sonnet"], summ["sonnet_raw"]
        L += [
            "",
            "## What the cleaning layer is worth",
            (
                "Same model, same questions. The raw run sees only what the customer handed over (`raw.*`) through "
                "`describe_data` and `run_sql`; the forecasting, anomaly, variance and reorder tools don't exist "
                "without the cleaned layer, so questions that need them fail by construction. The fair comparison is "
                "the data questions:"
            ),
            "",
            "| Questions | Cleaned layer | Raw data |",
            "|---|---|---|",
            f"| T1 + T2 (lookups and aggregations, 60) | {pct(s['T1+T2'])} | {pct(r['T1+T2'])} |",
            f"| All 120 | {pct(s['overall'])} | {pct(r['overall'])} |",
            "",
            "By question type (T1 + T2), where the injected problems live:",
            "",
            "| Question type | n | Cleaned | Raw |",
            "|---|---:|---:|---:|",
        ]
        kinds: dict[str, list[str]] = {}
        for q in res["sonnet"].values():
            if q["tier"] in ("T1", "T2"):
                kinds.setdefault(q["kind"], []).append(q["id"])
        for k, ids in sorted(kinds.items()):
            c = sum(res["sonnet"][i]["correct"] for i in ids)
            rr = sum(res["sonnet_raw"][i]["correct"] for i in ids if i in res["sonnet_raw"])
            L.append(f"| `{k}` | {len(ids)} | {c}/{len(ids)} | {rr}/{len(ids)} |")

    for n in [x for x in ("sonnet", "haiku") if x in res]:
        fails = [r for r in res[n].values() if not r["correct"]]
        L += ["", f"## Every miss: {LABELS[n]} ({len(fails)})", ""]
        if not fails:
            L.append("None.")
            continue
        L += [
            "| Id | Kind | Question | Expected | Agent's ANSWER | Why it failed |",
            "|---|---|---|---|---|---|",
        ]
        for r in sorted(fails, key=lambda r: r["id"]):
            exp = r["expected"] if not isinstance(r["expected"], list) else ", ".join(map(str, r["expected"]))
            L.append(f"| {r['id']} | `{r['kind']}` | {r['question'][:110].replace('|', '/')} | {exp} | "
                     f"{(r['answer_line'] or '(none)')[:60].replace('|', '/')} | {r['reason'][:80].replace('|', '/')} |")  # fmt: skip
    L += [
        "",
        "## How to read this",
        (
            "- **Scoring is deterministic** (ADR 0005): each answer ends with an `ANSWER:` line checked in code against a "
            "value computed from `ground_truth.duckdb`, or, for traps, against required and forbidden behaviour. No LLM "
            "grades the headline number."
        ),
        (
            "- **Questions are templated** with parameters sampled by a fixed seed, and deliberately concentrated on the "
            "injected data problems. They test this agent on this data; they are not a general benchmark."
        ),
        (
            "- **Forecast and reorder questions** check that the agent uses the tools correctly (the expected value is "
            "the model's own output). Forecast accuracy is measured separately in `forecast_backtest.md`."
        ),
        (
            "- **One run per configuration.** Model sampling varies between runs; a re-run can move a tier by a question "
            "or two, which is inside the intervals shown."
        ),
    ]
    OUT_MD.write_text("\n".join(L) + "\n")
    print(f"wrote {OUT_MD} and {OUT_PNG}")
    for n in order:
        print(f"{LABELS[n]:42} overall {summ[n]['overall'][0]:.1%}  ${summ[n]['cost_total']:.2f}")


if __name__ == "__main__":
    main()
