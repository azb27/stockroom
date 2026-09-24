"""Re-score stored answers with the current questions and scorers, without calling the API.

    python -m evals.rescore sonnet haiku sonnet_raw

Only valid where the question text is unchanged. Answers to reworded questions are removed so that
`python -m evals.run --config <name>` re-runs exactly those (it skips ids already present).
"""

from __future__ import annotations

import json
import sys

from evals.run import load_questions
from evals.scoring import score
from stockroom import config


def rescore(name: str) -> None:
    path = config.RUNS_DIR / "evals" / name / "results.jsonl"
    qs = {q["id"]: q for q in load_questions()}
    kept, dropped, flipped = [], [], []
    for line in path.read_text().splitlines():
        r = json.loads(line)
        q = qs[r["id"]]
        if q["question"] != r["question"]:
            dropped.append(r["id"])
            continue
        was = r["correct"]
        ok, why, ans = score(q, r["answer"]) if not r.get("error") else (False, r["reason"], None)
        r.update(
            {k: q[k] for k in ("expected", "scorer", "tol", "kind")}, correct=ok, reason=why, answer_line=ans
        )
        if ok != was:
            flipped.append(f"{r['id']} {'FAIL->PASS' if ok else 'PASS->FAIL'}")
        kept.append(json.dumps(r, default=str) + "\n")
    path.write_text("".join(kept))
    print(
        f"{name}: rescored {len(kept)}, flipped {flipped or 'none'}, removed for re-run {dropped or 'none'}"
    )


if __name__ == "__main__":
    for n in sys.argv[1:]:
        rescore(n)
