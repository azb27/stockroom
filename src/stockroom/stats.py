"""Small, dependency-free statistics used by the forecast backtest and (P5) the agent evals."""

from __future__ import annotations

from math import comb

import numpy as np


def bootstrap_ci(
    stat_fn,
    n_units: int,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 27,
) -> tuple[float, float, float]:
    """Percentile bootstrap over resampled unit indices.

    `stat_fn(idx)` computes the statistic on the units selected by the integer index array `idx`
    (with repeats). Returns (point_estimate, lower, upper).
    """
    rng = np.random.default_rng(seed)
    point = float(stat_fn(np.arange(n_units)))
    draws = np.empty(n_boot)
    for b in range(n_boot):
        draws[b] = stat_fn(rng.integers(0, n_units, n_units))
    lo, hi = np.quantile(draws, [alpha / 2, 1 - alpha / 2])
    return point, float(lo), float(hi)


def wape(actual: np.ndarray, pred: np.ndarray) -> float:
    """Weighted absolute percentage error: sum|y - yhat| / sum y."""
    denom = float(np.sum(actual))
    return float(np.sum(np.abs(actual - pred)) / denom) if denom else float("nan")


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for paired binary outcomes.

    b = cases A right and B wrong, c = cases A wrong and B right.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * p)
