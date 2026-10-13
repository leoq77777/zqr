"""Statistical validation of Sharpe ratios.

- Probabilistic Sharpe Ratio (Bailey & López de Prado, 2012): P(true SR > benchmark) given sample length,
  skewness and kurtosis of per-bar returns.
- Deflated Sharpe Ratio (Bailey & López de Prado, 2014): PSR against the Sharpe expected from the best of N
  independent trials, where N is the cumulative number of research trials on the split.
- Circular block bootstrap confidence intervals, deterministic (fixed seed) so results replay exactly.
"""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any

import numpy as np

EULER_GAMMA = 0.5772156649015329
_N = NormalDist()
BOOTSTRAP_SAMPLES = 500
BOOTSTRAP_SEED = 0
MIN_TRIALS_FOR_EMPIRICAL_VARIANCE = 5


def _moments(r: np.ndarray) -> tuple[float, float, float]:
    """Per-bar Sharpe, skewness and (non-excess) kurtosis."""
    mu, sd = r.mean(), r.std(ddof=1)
    if sd == 0 or len(r) < 3:
        return 0.0, 0.0, 3.0
    z = (r - mu) / r.std(ddof=0)
    return float(mu / sd), float((z**3).mean()), float((z**4).mean())


def probabilistic_sharpe(sr: float, benchmark_sr: float, n: int, skew: float, kurt: float) -> float:
    """sr and benchmark_sr are per-bar (not annualised)."""
    if n < 2:
        return 0.5
    var = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr**2
    z = (sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(max(var, 1e-12))
    return _N.cdf(z)


def expected_max_sharpe(n_trials: int, trial_sr_variance: float) -> float:
    """Expected maximum per-bar Sharpe among n_trials skill-less strategies."""
    if n_trials <= 1 or trial_sr_variance <= 0:
        return 0.0
    return math.sqrt(trial_sr_variance) * ((1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n_trials)
                                           + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n_trials * math.e)))


def _block_bootstrap(r: np.ndarray, ppy: float) -> dict[str, float]:
    n = len(r)
    block = max(1, round(n ** (1 / 3)))
    n_blocks = -(-n // block)
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    sharpes, totals = [], []
    for chunk in range(0, BOOTSTRAP_SAMPLES, 100):
        b = min(100, BOOTSTRAP_SAMPLES - chunk)
        starts = rng.integers(0, n, size=(b, n_blocks))
        idx = ((starts[:, :, None] + np.arange(block)) % n).reshape(b, -1)[:, :n]
        sample = r[idx]
        sd = sample.std(axis=1, ddof=1)
        sharpes.append(np.where(sd > 0, sample.mean(axis=1) / np.where(sd > 0, sd, 1), 0.0) * math.sqrt(ppy))
        totals.append(np.expm1(np.log1p(sample).sum(axis=1)))
    s, t = np.concatenate(sharpes), np.concatenate(totals)
    return {
        "bootstrap_samples": BOOTSTRAP_SAMPLES,
        "bootstrap_block": block,
        "sharpe_ci_05": float(np.percentile(s, 5)),
        "sharpe_ci_95": float(np.percentile(s, 95)),
        "total_return_ci_05": float(np.percentile(t, 5)),
        "total_return_ci_95": float(np.percentile(t, 95)),
        "prob_sharpe_le_0": float((s <= 0).mean()),
    }


def sharpe_statistics(returns: np.ndarray, ppy: float) -> dict[str, Any]:
    """Trial-independent statistics; deterministic for a given return series."""
    r = np.asarray(returns, dtype=float)
    sr, skew, kurt = _moments(r)
    out: dict[str, Any] = {
        "n_bars": int(len(r)),
        "sharpe_per_bar": sr,
        "skewness": skew,
        "kurtosis": kurt,
        "psr_0": probabilistic_sharpe(sr, 0.0, len(r), skew, kurt),
    }
    if len(r) >= 10:
        out.update(_block_bootstrap(r, ppy))
    return out


def deflate(stats: dict[str, Any], n_trials: int, trial_sharpes_annual: list[float], ppy: float) -> dict[str, Any]:
    """Add the Deflated Sharpe Ratio given the research trials made so far on the same split."""
    n = stats["n_bars"]
    per_bar = [s / math.sqrt(ppy) for s in trial_sharpes_annual]
    if len(per_bar) >= MIN_TRIALS_FOR_EMPIRICAL_VARIANCE:
        variance, source = float(np.var(per_bar, ddof=1)), "empirical across trials"
    else:
        variance, source = 1.0 / max(n - 1, 1), "null estimator 1/(T-1) (too few completed trials)"
    sr0 = expected_max_sharpe(max(n_trials, 1), variance)
    return {
        **stats,
        "n_trials": int(n_trials),
        "trial_sharpe_variance_source": source,
        "expected_max_sharpe_annual": sr0 * math.sqrt(ppy),
        "dsr": probabilistic_sharpe(stats["sharpe_per_bar"], sr0, n, stats["skewness"], stats["kurtosis"]),
    }
