"""USD per million tokens, from https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-09-24).

Used to enforce the per-conversation cost cap and to report $/question in evals. If a model is
missing here the agent refuses to run, rather than silently reporting $0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-5": {
        "input": 2.0,
        "output": 10.0,
        "cache_write_5m": 2.5,
        "cache_write_1h": 4.0,
        "cache_read": 0.20,
    },
    "claude-haiku-4-5-20251001": {
        "input": 1.0,
        "output": 5.0,
        "cache_write_5m": 1.25,
        "cache_write_1h": 2.0,
        "cache_read": 0.10,
    },
    "claude-opus-5-5": {
        "input": 4.0,
        "output": 20.0,
        "cache_write_5m": 5.0,
        "cache_write_1h": 8.0,
        "cache_read": 0.20,
    },
}


# models that accept output_config.effort (adaptive thinking); others reject it with a 400
EFFORT_MODELS = {"claude-sonnet-5", "claude-opus-5-5"}


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    thinking: int = 0
    requests: int = 0

    def add(self, u: Any) -> None:
        self.input += getattr(u, "input_tokens", 0) or 0
        self.output += getattr(u, "output_tokens", 0) or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        details = getattr(u, "output_tokens_details", None)
        self.thinking += (getattr(details, "thinking_tokens", 0) or 0) if details else 0
        self.requests += 1

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


def check_model(model: str) -> None:
    if model not in PRICES:
        raise ValueError(f"no price for {model!r}; add it to agent/pricing.py before using it")


def cost_usd(model: str, u: Usage) -> float:
    """Cost of accumulated usage. Cache writes are priced at the 5-minute rate (what the agent uses)."""
    p = PRICES[model]
    return (
        u.input * p["input"]
        + u.output * p["output"]
        + u.cache_write * p["cache_write_5m"]
        + u.cache_read * p["cache_read"]
    ) / 1e6
