"""Audit probes: passing means the described defect is reproduced, NOT fixed.

Run from repository root: .venv/bin/python -m pytest -q -s <this file>
Only synthetic data and temporary directories; no production/model calls.
"""
import json
from decimal import Decimal

import numpy as np

from quant_research.backtest.engine import CostConfig, EngineConfig
from quant_research.campaign import budget
from quant_research.campaign import store as campaign_module
from quant_research.campaign.store import CampaignStore
from quant_research.data.validate import validate_bars
from quant_research.paper.risk import RiskLimits
from quant_research.paper.runner import run_replay
from quant_research.portfolio.backtest import run_portfolio
from quant_research.portfolio.paper import request_resume
from quant_research.portfolio.version import Member
from tests.conftest import HOUR, T0, make_raw
from tests.conftest import dataset as dataset
from tests.conftest import paths as paths
from tests.conftest import store as store
from tests.test_campaign import FakeAgent, _loop, _queued
from tests.test_hypothesis import _reference
from tests.test_paper import PRODUCT
from tests.test_portfolio_paper import _bars, _cfg, _crash_trader, _deliver, _version


def emit(name, **evidence):
    print(json.dumps({"probe": name, **evidence}, sort_keys=True))


def test_order_cap_after_growth_leaves_target_unfilled(tmp_path):
    close = np.array([100, 101, 102, 200, 300, 290, 280, 270, 260, 250, 251, 252, 253, 254], float)
    bars = _bars(len(close))
    bars["close"] = close
    bars["open"] = np.r_[close[0], close[:-1]]
    bars["high"] = np.maximum(bars.open, bars.close)
    bars["low"] = np.minimum(bars.open, bars.close)
    v = _version((Member(entry_id="a", family="trend", strategy_ref="template:sma_cross",
                         parameters={"fast": 1, "slow": 2}),),
                 engine_config=EngineConfig(costs=CostConfig(fee_rate=0, slippage_bps=0)))
    cfg = _cfg(v, fee_rate=Decimal(0), slippage_bps=Decimal(0),
               risk=RiskLimits(max_order_notional=10000, max_orders_per_hour=100,
                               max_daily_loss_fraction=0.99))
    t = run_replay(cfg, bars, tmp_path / "growth", PRODUCT)
    equity = float(t.equity(Decimal(str(close[-1]))))
    position = float(t.balances()["BTC"]) * close[-1]
    bt = run_portfolio(v, bars)
    decisions = t.ledger.decisions()
    assert t.decider.state.last_target == 1
    assert position / equity < 0.5
    assert all(d["intent"] is None for d in decisions[-2:])
    emit("R1_cap_and_last_target", last_target=t.decider.state.last_target,
         actual_fraction=position/equity, paper_equity=equity,
         backtest_equity=float(bt.result.equity.equity.iloc[-1]),
         last_two_order_targets=[d["portfolio"]["order_target"] for d in decisions[-2:]])
    t.ledger.close()


def test_family_quota_overshoots_inside_session(paths, store, dataset, monkeypatch):
    _reference(paths, store, dataset)
    monkeypatch.setattr(campaign_module, "FAMILY_QUOTA", 1)
    cid = _queued(paths, store)
    loop, _ = _loop(paths, store, FakeAgent(paths, store))
    out = loop.step()
    q = CampaignStore(store).quota(paths, dataset[1].split_id, "tsmom")
    assert out["ran"] and q["used"] > q["quota"]
    emit("R2_family_quota", used=q["used"], quota=q["quota"],
         session_allowance=CampaignStore(store).sessions(cid)[0]["allowance"])


def test_resume_needs_neither_30_days_nor_review(paths, monkeypatch):
    t, rows, clock = _crash_trader(paths.paper / "audit-resume")
    for row in rows[:420]:
        _deliver(t, clock, row)
    assert t.decider.state.level == "liquidated"
    # The synthetic replay spans under 18 days in total; no review artifact exists.
    monkeypatch.setattr("quant_research.portfolio.paper.time.time", lambda: clock["now"])
    result = request_resume(paths, "audit-resume", "go")
    assert result["requested"]["note"] == "go"
    emit("R3_resume", accepted=True, review_supplied=False, review_note="go")
    t.ledger.close()


def test_missing_usage_is_booked_as_free(paths, store):
    loop, _ = _loop(paths, store, None)
    cost, source = loop._cost(None, None)
    loop._book_stop("crash-without-usage", cost, source, 0.25, None)
    assert (cost, source) == (0, "no_tokens")
    assert budget.active_stop(paths) is None
    emit("R4_unknown_usage", cost=cost, source=source, budget_stop=False)


def test_flash_crash_wick_erases_whole_bar():
    raw = make_raw(3)
    raw[["open", "high", "low", "close"]] = 315.97
    raw.loc[1, ["open", "high", "low", "close"]] = [315.97, 320, 0.10, 298.0]
    bars, report = validate_bars(raw, "coinbase", "ETH-USD", "ONE_HOUR", T0, T0 + 3 * HOUR,
                                 fill_policy="ffill_flat", drop_isolated_prints=True)
    assert report.invalid_dropped["isolated_print"] == 1
    assert bars.iloc[1]["close"] == 315.97 and bars.iloc[1]["volume"] == 0
    emit("R5_flash_crash_cleaning", original_close=298, clean_close=float(bars.iloc[1]["close"]),
         original_low=0.1, clean_low=float(bars.iloc[1]["low"]), clean_volume=float(bars.iloc[1]["volume"]))
