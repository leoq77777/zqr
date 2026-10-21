"""Golden cases and contract checks from docs/contracts-v2.md (sections 3-5)."""

import math

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from quant_research.backtest.costs import RETAIL_TAKER_PROFILE, cost_config
from quant_research.backtest.engine import EngineConfig, run_backtest
from quant_research.experiments.replay import replay_experiment
from quant_research.experiments.runner import ExperimentRequest, run_experiment
from quant_research.portfolio.policy import (
    IDENTITY_POLICY,
    PortfolioPolicy,
    apply_schedule,
    cap_gross,
    net_proposals,
    single_asset_weights,
    vol_scale,
)
from quant_research.strategy.plugin import register_plugin

# Golden cases in contracts-v2 §4 were hand-worked under the retail-taker fee schedule.
_G1_ENGINE = EngineConfig(costs=cost_config(RETAIL_TAKER_PROFILE))

BARS = pd.DataFrame({"bar_open_time": pd.to_datetime([0, 3600, 7200, 10800], unit="s", utc=True),
                     "open": [100.0, 100.0, 110.0, 100.0], "close": [100.0, 110.0, 99.0, 100.0]})
G1_TARGETS = np.array([1.0, 1.0, 0.5, 0.0])


def _close(a, b, tol=1e-6):
    return math.isclose(a, b, abs_tol=tol)


def test_g1_hand_worked_fills():
    bt = run_backtest(BARS, G1_TARGETS, _G1_ENGINE)
    buy, sell = bt.fills.iloc[0], bt.fills.iloc[1]
    assert (buy.bar_index, buy.side) == (1, "buy") and _close(buy.qty, 99.35390158, 1e-8)
    assert _close(buy.price, 100.05) and _close(buy.notional, 9940.357853) and _close(buy.fee, 59.642147)
    assert (sell.bar_index, sell.side) == (3, "sell") and _close(sell.qty, 49.67695079, 1e-8)
    assert _close(sell.price, 99.95) and _close(sell.notional, 4965.211231) and _close(sell.fee, 29.791267)
    eq = bt.equity["equity"].to_numpy()
    for got, want in zip(eq, [10000.0, 10928.929174, 9836.036256, 9903.115043], strict=True):
        assert _close(got, want)
    assert len(bt.fills) == 2  # the 0.0 decided on the final bar is never filled


def test_g2_quantity_rounding_and_liquidation_equity():
    bt = run_backtest(BARS, G1_TARGETS, _G1_ENGINE.model_copy(update={"base_increment": 0.01}))
    assert _close(bt.fills.qty.iloc[0], 99.35) and _close(bt.fills.notional.iloc[0], 9939.9675)
    assert _close(bt.fills.qty.iloc[1], 49.67) and _close(bt.fills.notional.iloc[1], 4964.5165)
    last = bt.equity.iloc[-1]
    assert _close(last.cash, 4935.122096) and _close(last.qty, 49.68) and _close(last.equity, 9903.122096)
    # final position sold at the last close with slippage and fee
    assert _close(bt.meta["liquidation_equity"], 4935.122096 + 49.68 * 100 * 0.9995 * 0.994)


def test_g3_insufficient_cash_caps_the_rebalance():
    bt = run_backtest(BARS, np.array([0.5, 1.0, 1.0, 1.0]), _G1_ENGINE)
    assert _close(bt.fills.notional.iloc[0], 5000.0) and _close(bt.fills.fee.iloc[0], 30.0)
    # at b2 the wanted buy is 4970 but only 4970 / 1.006 is affordable after the fee
    assert _close(bt.fills.notional.iloc[1], 4940.357853) and _close(bt.fills.fee.iloc[1], 29.642147)
    assert bt.equity.cash.min() >= 0 and _close(bt.equity.equity.iloc[2], 9391.626254)


def test_delay_and_adverse_fill():
    delayed = run_backtest(BARS, G1_TARGETS, _G1_ENGINE.model_copy(update={"execution_delay_bars": 1}))
    assert list(delayed.fills.bar_index) == [2] and _close(delayed.fills.price.iloc[0], 110.055)
    adverse = run_backtest(BARS, G1_TARGETS, _G1_ENGINE.model_copy(update={"adverse_fill": True}))
    # sell at b3: reference is min(open 100, decision close 99) before slippage
    assert _close(adverse.fills.price.iloc[1], 99 * 0.9995) and _close(adverse.fills.notional.iloc[1], 4915.559119)
    cfg = _G1_ENGINE
    assert cfg.sensitivity("delay_1_adverse").execution_delay_bars == 1
    assert cfg.sensitivity("costs_x2").costs.fee_rate == 2 * cfg.costs.fee_rate


def test_nan_target_sends_no_order():
    bt = run_backtest(BARS, np.array([1.0, np.nan, 0.0, np.nan]), _G1_ENGINE)
    assert list(bt.fills.bar_index) == [1, 3]


def _alternating_close(n, daily_sd):
    a = daily_sd * math.sqrt((n - 1) / n)  # ddof=1 std of n alternating +-a returns equals daily_sd (n even)
    return 100 * np.exp(np.cumsum([0.0] + [a if i % 2 else -a for i in range(n)]))


def test_p1_vol_target_netting_and_gross_cap():
    close = _alternating_close(30, 0.03)
    times = np.arange(len(close)) * 86400
    policy = PortfolioPolicy(vol_target=0.40, vol_lookback=30)
    w = single_asset_weights(policy, np.ones(len(close)), close, times, 86400, 365)
    assert (w[:30] == 0).all()  # warmup
    assert _close(w[30], 0.40 / (0.03 * math.sqrt(365)), 1e-9) and _close(w[30], 0.6979, 1e-4)
    raw = net_proposals([(0.5, np.ones(len(close))), (0.5, np.zeros(len(close)))])
    assert _close(single_asset_weights(policy, raw, close, times, 86400, 365)[30], 0.3489, 1e-4)
    capped = cap_gross({"BTC": np.array([0.8]), "ETH": np.array([0.6])}, 1.0)
    assert _close(capped["BTC"][0], 0.8 / 1.4) and _close(capped["ETH"][0], 0.6 / 1.4)
    assert _close(vol_scale(policy, np.array([0.2]))[0], 1.0)  # never levers up


def test_policy_is_prefix_causal_and_schedule_is_epoch_anchored():
    rng = np.random.default_rng(0)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 200)))
    times = np.arange(200) * 86400
    exp = (rng.random(200) > 0.5).astype(float)
    policy = PortfolioPolicy(vol_target=0.5, vol_lookback=20, asset_cap=0.8, rebalance_every=3)
    full = single_asset_weights(policy, exp, close, times, 86400, 365)
    for t in (25, 77, 150, 199):
        part = single_asset_weights(policy, exp[: t + 1], close[: t + 1], times[: t + 1], 86400, 365)
        assert np.array_equal(part[-1], full[t], equal_nan=True)
    assert np.isnan(full[1]) and not np.isnan(full[3]) and np.nanmax(full) <= 0.8
    assert np.isnan(apply_schedule(np.ones(3), np.array([86400, 2 * 86400, 3 * 86400]), 86400, 3)[:2]).all()


def test_identity_policy_validation_and_round_trip():
    with pytest.raises(ValidationError):
        PortfolioPolicy(name="identity", vol_target=0.4)
    assert PortfolioPolicy.model_validate(IDENTITY_POLICY.model_dump()) == IDENTITY_POLICY
    assert IDENTITY_POLICY.label() == "identity-v1" and PortfolioPolicy().policy_id() != IDENTITY_POLICY.policy_id()


EXPOSURE_PLUGIN = """
def generate_exposures(history, parameters):
    c = history["close"]
    return (c > c.rolling(parameters["n"]).mean()).astype(float)
"""
NAN_EXPOSURE_PLUGIN = """
def generate_exposures(history, parameters):
    c = history["close"]
    return (c / c.rolling(parameters["n"]).mean()).clip(0, 1).where(c.notna())
"""
LEGACY_PLUGIN = """
def target_positions(bars, params):
    c = bars["close"]
    return (c > c.rolling(params["n"]).mean()).astype(float)
"""
V01_PLUGIN = """
def generate_targets(bars, params):
    out, closes = [], []
    for b in bars:
        closes.append(b["close"])
        window = closes[-params["n"]:]
        out.append(1.0 if len(window) == params["n"] and b["close"] > sum(window) / params["n"] else 0.0)
    return out
"""


def _ref(paths, tmp_path, name, code):
    f = tmp_path / f"{name}.py"
    f.write_text(code)
    return f"plugin:{register_plugin(paths, f)}"


def _request(dataset, **kw):
    manifest, split = dataset
    base = {"dataset_id": manifest["dataset_id"], "split_id": split.split_id, "parameters": {"n": 20}}
    return ExperimentRequest(**{**base, **kw})


def test_exposure_and_legacy_contracts(paths, store, dataset, tmp_path):
    vol = PortfolioPolicy(vol_target=0.5)
    exposures = run_experiment(paths, store, _request(dataset, strategy_ref=_ref(paths, tmp_path, "e", EXPOSURE_PLUGIN),
                                                      policy=vol))
    assert exposures.status == "completed", exposures.failure_reason
    assert exposures.checks["strategy_contract"] == "exposures"
    assert "vol_target=0.5" in exposures.checks["policy_applied"]
    assert replay_experiment(paths, store, exposures.experiment_id)["reproducible"]

    legacy_ref = _ref(paths, tmp_path, "l", LEGACY_PLUGIN)
    legacy = run_experiment(paths, store, _request(dataset, strategy_ref=legacy_ref))
    assert legacy.status == "completed" and legacy.checks["policy_applied"] == "identity-v1"
    refused = run_experiment(paths, store, _request(dataset, strategy_ref=legacy_ref, policy=vol))
    assert refused.status == "rejected" and "generate_exposures" in refused.failure_reason

    v01 = run_experiment(paths, store, _request(dataset, strategy_ref=_ref(paths, tmp_path, "v", V01_PLUGIN)))
    assert v01.status == "completed", v01.failure_reason
    assert v01.metrics["total_return"] == pytest.approx(legacy.metrics["total_return"])

    nan = run_experiment(paths, store, _request(dataset, strategy_ref=_ref(paths, tmp_path, "n", NAN_EXPOSURE_PLUGIN)))
    assert nan.status == "failed" and "NaN" in nan.failure_reason


def test_policy_matched_baseline_and_policy_in_duplicate_identity(paths, store, dataset):
    req = _request(dataset, strategy_ref="template:sma_cross", parameters={"fast": 10, "slow": 50})
    res = run_experiment(paths, store, req)
    pm = res.benchmarks["policy_matched"]
    assert 0 < pm["proposal"] < 1 and _close(pm["proposal"], res.metrics["exposure"], 0.05)
    assert run_experiment(paths, store, req).status == "duplicate"
    other = run_experiment(paths, store, req.model_copy(update={"policy": PortfolioPolicy(vol_target=0.5)}))
    assert other.status == "completed"
    assert store.get_experiment(other.experiment_id)["policy_id"] == PortfolioPolicy(vol_target=0.5).policy_id()
