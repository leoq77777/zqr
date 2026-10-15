"""Split statistics on daily returns (contracts v2 appendix A, section A4)."""

import numpy as np
import pytest

from quant_research.evaluation import split_stats as ss

T = 1267


def test_sigma0_and_sr0_reference_values():
    assert ss.sigma0(T) == pytest.approx(0.5369, abs=1e-4)
    expected = {1: 0.0, 2: 0.279, 3: 0.458, 20: 1.021, 110: 1.377, 260: 1.530}
    for k, v in expected.items():
        assert ss.sr0(k, T) == pytest.approx(v, abs=1e-3)


def test_needed_sharpe_for_dsr_0_9_matches_appendix_table():
    needed = {1: 0.688, 3: 1.146, 20: 1.709, 260: 2.219}
    for k, v in needed.items():
        assert ss.sr0(k, T) + 1.2815515655 * ss.sigma0(T) == pytest.approx(v, abs=1e-3)


def test_sigma_is_floored_at_the_null_on_a_skewed_series():
    r = np.full(T, -0.0002)
    r[100] = 0.9  # one day dominates: skewness 35.6
    s = ss.sharpe_daily(r)
    assert ss.sigma_sr(r) < ss.sigma0(T)  # the fat-tail formula alone would be lenient
    assert ss.dsr_k(r, 1) == pytest.approx(ss.norm_cdf(s / ss.sigma0(T)))


def test_flat_series_has_zero_sharpe_and_dsr_one_half_at_k1():
    r = np.zeros(T)
    assert ss.sharpe_daily(r) == 0.0 and ss.dsr_k(r, 1) == 0.5


def test_dsr_falls_as_k_grows():
    r = np.random.default_rng(1).normal(0.002, 0.03, T)
    values = [ss.dsr_k(r, k) for k in (1, 3, 20, 260)]
    assert values == sorted(values, reverse=True)


def test_appraisal_test_passes_an_uncorrelated_modest_variant_and_fails_a_redundant_strong_one():
    rng = np.random.default_rng(0)
    p = rng.normal(0.004, 0.03, T)  # portfolio, realised SR 1.80
    modest = rng.normal(0.0016, 0.02, T)  # independent, SR 1.09, AR 1.17, statistic 0.985
    redundant = 1.2 * p + rng.normal(0.0, 0.002, T)  # SR 1.80 but a copy: AR 0.05, statistic 0.54
    good, bad = ss.appraisal_test(modest, p, k=1), ss.appraisal_test(redundant, p, k=1)
    assert good["statistic"] >= 0.9 and good["passed"]
    assert bad["statistic"] < 0.9 and not bad["passed"]
    assert bad["beta"] == pytest.approx(1.2, abs=0.01)


def test_appraisal_test_penalises_more_registered_variants():
    rng = np.random.default_rng(0)
    p = rng.normal(0.004, 0.03, T)
    c = rng.normal(0.0016, 0.02, T)
    stats = [ss.appraisal_test(c, p, k=k)["statistic"] for k in (1, 2, 3)]
    assert stats == sorted(stats, reverse=True)


def test_appraisal_test_refuses_more_than_three_variants():
    with pytest.raises(ValueError):
        ss.appraisal_test(np.zeros(T), np.zeros(T), k=4)
