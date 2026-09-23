"""The shared statistics used to make claims in docs/results must themselves be right."""

from __future__ import annotations

import numpy as np
import pytest

from stockroom.stats import bootstrap_ci, mcnemar_exact, wape


def test_wape():
    assert wape(np.array([10.0, 0.0, 5.0]), np.array([8.0, 1.0, 5.0])) == pytest.approx(3 / 15)
    assert np.isnan(wape(np.zeros(3), np.ones(3)))


def test_bootstrap_ci_of_a_mean_matches_theory():
    rng = np.random.default_rng(0)
    x = rng.normal(10, 2, 400)
    point, lo, hi = bootstrap_ci(lambda i: x[i].mean(), len(x), n_boot=3000)
    se = x.std(ddof=1) / np.sqrt(len(x))
    assert point == pytest.approx(x.mean())
    assert lo == pytest.approx(x.mean() - 1.96 * se, abs=0.03)
    assert hi == pytest.approx(x.mean() + 1.96 * se, abs=0.03)


def test_bootstrap_is_reproducible():
    x = np.arange(50.0)
    assert bootstrap_ci(lambda i: x[i].mean(), 50) == bootstrap_ci(lambda i: x[i].mean(), 50)


def test_mcnemar_exact_known_values():
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(5, 5) == 1.0
    # b=10, c=0: p = 2 * 0.5**10
    assert mcnemar_exact(10, 0) == pytest.approx(2 * 0.5**10)
    # symmetric in b and c
    assert mcnemar_exact(3, 12) == pytest.approx(mcnemar_exact(12, 3))
