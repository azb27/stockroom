"""The public Space gets the app and cleaned data, never the eval's answer key or secrets."""

from __future__ import annotations

import importlib.util

import pytest

from stockroom import config

spec = importlib.util.spec_from_file_location("deploy_space", config.ROOT / "scripts" / "deploy_space.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def test_space_upload_is_the_docker_context_without_the_answer_key(tmp_path):
    names = deploy.stage(tmp_path, "https://github.com/x/y")
    assert {"Dockerfile", "README.md", "pyproject.toml", "docs/results/eval_summary.json"} <= set(names)
    assert not any("ground_truth" in n or "dirt_manifest" in n or n.endswith(".env") for n in names)
    assert not any(n.startswith(("web/node_modules", "web/out", "runs/", "evals/", "tests/")) for n in names)
    readme = (tmp_path / "README.md").read_text()
    assert "sdk: docker" in readme and "app_port: 7860" in readme


def test_staging_refuses_the_answer_key_even_if_the_allow_list_is_widened(tmp_path):
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    (root / ".dockerignore").write_text("*\n!data/\n")
    (root / "data" / "ground_truth.duckdb").write_bytes(b"x")
    with pytest.raises(SystemExit, match="refusing to publish"):
        deploy.stage(tmp_path / "out", "u", root=root)
