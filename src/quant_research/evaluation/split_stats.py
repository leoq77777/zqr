"""Statistics on daily returns for portfolio research (contracts v2 appendix A, section A4).

The luck dispersion is the null's (`sigma0`), not the spread of trial Sharpe ratios, which is dominated by trials
that lose deterministically to costs. The denominator is floored at the null so fat tails never make a test more
lenient.
"""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any

import numpy as np

DAYS = 365
EULER_GAMMA = 0.5772156649015329
MAX_REGISTERED = 3
PASS = 0.9
_N = NormalDist()
norm_cdf = _N.cdf


def sharpe_daily(r) -> float:
    """Annualised Sharpe of daily returns (sample standard deviation); 0 for a constant series."""
    r = np.asarray(r, float)
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    return 0.0 if sd == 0 else float(r.mean() / sd * math.sqrt(DAYS))


def _moments(r: np.ndarray) -> tuple[float, float]:
    """Skewness and (non-excess) kurtosis, population moments."""
    sd0 = r.std(ddof=0)
    if sd0 == 0:
        return 0.0, 3.0
    z = (r - r.mean()) / sd0
    return float((z**3).mean()), float((z**4).mean())


def sigma0(t: int) -> float:
    """Standard error of the annualised Sharpe under the null, for t daily returns."""
    return math.sqrt(DAYS / (t - 1))


def sigma_sr(r) -> float:
    """Standard error at the estimate (skewness and kurtosis adjusted); the null's when the formula breaks down."""
    r = np.asarray(r, float)
    s = sharpe_daily(r) / math.sqrt(DAYS)
    g3, g4 = _moments(r)
    term = 1 - g3 * s + (g4 - 1) / 4 * s * s
    return sigma0(len(r)) if term <= 0 else math.sqrt(term / (len(r) - 1)) * math.sqrt(DAYS)


def sr0(k: int, t: int) -> float:
    """Expected maximum annualised Sharpe of k skill-less trials of t daily returns."""
    if k <= 1:
        return 0.0
    return sigma0(t) * ((1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / k)
                        + EULER_GAMMA * _N.inv_cdf(1 - 1 / (k * math.e)))


def dsr_k(r, k: int) -> float:
    r = np.asarray(r, float)
    return norm_cdf((sharpe_daily(r) - sr0(k, len(r))) / max(sigma0(len(r)), sigma_sr(r)))


def appraisal_test(candidate, portfolio, k: int) -> dict[str, Any]:
    """Registered test (appendix A6, gate 1): the variant's return beyond its exposure to the portfolio."""
    if not 1 <= k <= MAX_REGISTERED:
        raise ValueError(f"a registered hypothesis has 1 to {MAX_REGISTERED} variants, not {k}")
    c, p = np.asarray(candidate, float), np.asarray(portfolio, float)
    var_p = p.var(ddof=1)
    beta = float(np.cov(c, p, ddof=1)[0, 1] / var_p) if var_p > 0 else 0.0
    h = c - beta * p
    stat = dsr_k(h, k)
    return {"beta": beta, "alpha_daily": float(h.mean()), "appraisal_ratio": sharpe_daily(h), "k": k,
            "statistic": stat, "passed": stat >= PASS}
