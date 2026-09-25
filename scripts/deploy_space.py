"""Publish the demo to a Hugging Face Space (Docker SDK). HF builds the image from the uploaded Dockerfile.

    HF_TOKEN=... python scripts/deploy_space.py [--space stockroom] [--dry-run]

What gets uploaded is exactly the Docker build context in `.dockerignore`, plus a Space README. That is
the app, the cleaned warehouse and the forecasts. `ground_truth.duckdb` and the dirt manifest are
refused even if someone edits the list. Files in a public Space are public.

The Anthropic key is NOT handled here. Add it yourself as a Space secret named ANTHROPIC_API_KEY
(Space -> Settings -> Variables and secrets). Use a key from a separate workspace with a spend limit.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("ground_truth.duckdb", "dirt_manifest.json", ".env", "stockroom.env")

SPACE_README = """---
title: Stockroom
emoji: 📦
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Ops agent on messy distributor data, with evals
---

# Stockroom: live demo

An ops agent for a distributor's messy data. It answers questions, flags stock-outs, forecasts demand
and drafts purchase orders that a human approves. Every tool call is shown.

Code, evaluation (120 ground-truth questions with confidence intervals) and design notes:
{repo}

The demo is capped at a small daily API budget; when it's spent, chat reopens at 00:00 UTC.
Drafts and chats live in memory and reset when the Space restarts.
"""


def context_files(root: Path = ROOT) -> list[Path]:
    """Files the Docker build context would include, per .dockerignore (the `*` + `!allow` form we use)."""
    rules = [
        line.strip()
        for line in (root / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    allow = [r[1:].rstrip("/") for r in rules if r.startswith("!")]
    deny = [r for r in rules if not r.startswith("!") and r != "*"]
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if not any(rel == a or rel.startswith(a + "/") for a in allow):
            continue
        if any(fnmatch(rel, d) or any(fnmatch(part, d.strip("*/")) for part in rel.split("/")) for d in deny):
            continue
        out.append(p)
    return out


def stage(dest: Path, repo_url: str, root: Path = ROOT) -> list[str]:
    files = context_files(root)
    names = [f.relative_to(root).as_posix() for f in files]
    bad = [n for n in names if any(n.endswith(f) for f in FORBIDDEN)]
    if bad:
        raise SystemExit(f"refusing to publish {bad}: the answer key and secrets never leave this machine")
    for f, n in zip(files, names, strict=True):
        (dest / n).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest / n)
    (dest / "README.md").write_text(SPACE_README.format(repo=repo_url))
    return [*names, "README.md"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--space", default="stockroom", help="Space name under your HF account")
    ap.add_argument("--repo-url", default="https://github.com/azb27/stockroom")
    ap.add_argument("--dry-run", action="store_true", help="list what would be uploaded, upload nothing")
    a = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        names = stage(Path(tmp), a.repo_url)
        size = sum((Path(tmp) / n).stat().st_size for n in names) / 1e6
        print(f"{len(names)} files, {size:.1f} MB")
        if a.dry_run:
            print("\n".join(names))
            return
        from huggingface_hub import HfApi  # noqa: PLC0415  (only needed to publish)

        token = os.environ.get("HF_TOKEN")
        if not token:
            sys.exit("set HF_TOKEN (a write token) in the environment")
        api = HfApi(token=token)
        user = api.whoami()["name"]
        repo_id = f"{user}/{a.space}"
        api.create_repo(repo_id, repo_type="space", space_sdk="docker", exist_ok=True)
        api.upload_folder(
            repo_id=repo_id,
            repo_type="space",
            folder_path=tmp,
            commit_message="Deploy Stockroom demo",
            delete_patterns="*",  # the Space mirrors this upload exactly; stale files are removed
        )
    print(f"Space: https://huggingface.co/spaces/{repo_id}")
    print(f"App:   https://{user.lower()}-{a.space.lower()}.hf.space")
    print("Next: add the secret ANTHROPIC_API_KEY in the Space settings (not through this script).")


if __name__ == "__main__":
    main()
