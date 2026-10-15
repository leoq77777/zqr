import math

import numpy as np
import pandas as pd
import pytest

from quant_research.backtest.engine import EngineConfig, run_backtest
from quant_research.data.validate import validate_bars
from quant_research.portfolio.backtest import run_portfolio
from quant_research.portfolio.blend import Blend, blend, member_shares
from quant_research.portfolio.decide import Decider, DeciderState
from quant_research.portfolio.version import (
    Ladder,
    Member,
    Overlay,
    PortfolioVersion,
    VersionError,
    VersionStore,
)
from quant_research.strategy.compute import check_positions
from tests.conftest import HOUR, T0, make_raw


def _bars(n=1500, seed=3, drift=0.0002, vol=0.01):
    bars, _ = validate_bars(make_raw(n, seed=seed, drift=drift, vol=vol), "coinbase", "BTC-USD", "ONE_HOUR", T0,
                            T0 + n * HOUR)
    return bars


def _member(eid, family, ref="template:buy_and_hold", **params):
    return Member(entry_id=eid, family=family, strategy_ref=ref, parameters=params)


def _version(members, overlay=None, **kw):
    return PortfolioVersion(product="BTC-USD", dataset_id="ds", split_id="split-x", members=tuple(members),
                            overlay=overlay, engine_config=EngineConfig(), **kw)


# -- versions -------------------------------------------------------------------------------------

def test_version_id_is_a_hash_of_the_body():
    v = _version([_member("a", "f")])
    assert v.version_id().startswith("pv-") and v.version_id() == _version([_member("a", "f")]).version_id()
    assert _version([_member("a", "f")], band=0.05).version_id() != v.version_id()
    assert _version([_member("a", "f", "template:sma_cross", fast=5, slow=20)]).version_id() != v.version_id()
    assert _version([_member("a", "f")], ladder=Ladder(recover=-0.10)).version_id() != v.version_id()


def test_version_refuses_settings_paper_cannot_reproduce():
    with pytest.raises(ValueError, match="deadband"):
        PortfolioVersion(product="BTC-USD", dataset_id="ds", split_id="s", members=(_member("a", "f"),),
                         engine_config=EngineConfig(rebalance_tolerance=0.05))
    with pytest.raises(ValueError, match="duplicate"):
        _version([_member("a", "f"), _member("a", "g")])


def test_version_store_records_every_transition(store):
    vs = VersionStore(store)
    vid = vs.put(_version([_member("a", "f")]))
    assert vs.put(_version([_member("a", "f")])) == vid and vs.get(vid)["state"] == "draft"
    with pytest.raises(VersionError):
        vs.transition(vid, "frozen", "human")
    vs.transition(vid, "validated", "controller", {"sharpe": 1})
    with pytest.raises(VersionError):
        vs.transition(vid, "frozen", "controller")
    vs.transition(vid, "frozen", "human", note="ok")
    vs.transition(vid, "final_evaluated", "evaluator")
    with pytest.raises(VersionError):
        vs.transition(vid, "paper", "controller")
    row = vs.transition(vid, "paper", "human")
    assert row["state"] == "paper" and row["evidence"] == {"sharpe": 1}
    assert [e["to_state"] for e in vs.events(vid)] == ["draft", "validated", "frozen", "final_evaluated", "paper"]
    assert vs.version(vid) == _version([_member("a", "f")])


# -- blend ----------------------------------------------------------------------------------------

def test_member_shares_use_equal_sleeves_and_the_cap():
    fams = [("tsmom", 4), ("ewma", 3), ("breakout", 2), ("filter", 2)]
    v = _version([_member(f"{f}{k}", f) for f, n in fams for k in range(n)])
    s = member_shares(v)
    assert s["tsmom0"] == pytest.approx(0.0625) and s["ewma0"] == pytest.approx(1 / 12)
    assert s["breakout0"] == s["filter1"] == pytest.approx(0.125)
    capped = member_shares(_version([_member("a", "f"), _member("b", "g")]))
    assert capped == {"a": 0.15, "b": 0.15}
    prob = member_shares(_version([_member("a", "f"), Member(entry_id="b", family="f", strategy_ref="template:cash",
                                                              status="probation")], member_cap=1.0))
    assert prob == {"a": 0.5, "b": 0.25}


def test_blend_matches_hand_calculation_and_the_overlay_scales_it():
    bars = _bars(300)
    members = [_member("a", "f"), _member("b", "f", "template:cash"), _member("c", "g")]
    bl = blend(_version(members, member_cap=1.0), bars)
    assert np.allclose(bl.target, 0.75) and np.allclose(bl.families["f"], 0.25)
    flat = blend(_version(members, Overlay(entry_id="o", strategy_ref="template:cash"), member_cap=1.0), bars)
    assert np.allclose(flat.target, 0.0) and np.allclose(flat.pre_overlay, 0.75)


def test_blend_has_no_lookahead():
    bars = _bars(1500)
    v = _version([_member("a", "f", "template:sma_cross", fast=5, slow=20), _member("b", "g")],
                 Overlay(entry_id="o", strategy_ref="template:vol_target", parameters={"window": 720,
                                                                                      "target_vol": 0.4}))
    full = blend(v, bars).target
    assert full.min() >= 0 and full.max() <= 1 and len(set(np.round(full, 6))) > 3
    for i in check_positions(np.ones(len(bars), bool)):
        assert blend(v, bars.iloc[: i + 1]).target[-1] == pytest.approx(full[i])


# -- decision step --------------------------------------------------------------------------------

def _decider(**kw):
    return Decider(0.10, Ladder(), 1.0, **kw)


def test_price_drift_alone_never_trades():
    d = _decider(state=DeciderState(last_target=0.5, peak=100))
    assert math.isnan(d.decide(0.5, 100, 50)) and math.isnan(d.decide(0.5, 105, 59))


def test_band_and_full_exit():
    d = _decider()
    assert math.isnan(d.decide(0.05, 100, 0))  # the account starts in cash
    assert d.decide(0.5, 100, 0) == 0.5
    d.executed(0.5)
    assert math.isnan(d.decide(0.58, 100, 50)) and d.decide(0.61, 100, 50) == 0.61
    d.executed(0.05)
    assert d.decide(0.0, 100, 5) == 0.0
    d.executed(0.0)
    assert math.isnan(d.decide(0.0, 100, 0.5))  # dust below the minimum notional


def test_ladder_halves_recovers_and_liquidates():
    d = _decider()
    d.executed(d.decide(0.8, 100, 0))
    assert d.decide(0.8, 74.9, 60) == pytest.approx(0.4) and d.state.level == "halved" and d.state.yellow
    d.executed(0.4)
    assert math.isnan(d.decide(0.8, 80, 30))  # still halved, inside the band
    assert d.decide(0.8, 85.1, 30) == pytest.approx(0.8) and d.state.level == "full"
    d.executed(0.8)
    assert d.decide(0.8, 69.9, 55) == 0.0 and d.state.level == "liquidated"
    assert d.decide(0.9, 120, 40) == 0.0 and d.state.peak == 100  # halted: exits only, the peak is frozen
    d.executed(0.0)
    assert math.isnan(d.decide(0.9, 120, 0))
    d.resume(120)
    assert d.state.peak == 120 and d.decide(0.9, 120, 0) == pytest.approx(0.9) and d.state.level == "full"


def test_yellow_flag_is_reported_without_action():
    d = _decider(state=DeciderState(last_target=0.5, peak=100))
    assert math.isnan(d.decide(0.5, 79, 40)) and d.state.yellow and d.state.level == "full"


def test_state_round_trips_through_a_dict():
    import json

    d = _decider()
    d.decide(0.5, np.float64(100.0), 0.0)
    s = d.state
    assert DeciderState.from_dict(json.loads(json.dumps(s.to_dict()))) == s
    assert DeciderState.from_dict(None) == DeciderState()


# -- engine hook and portfolio backtest -----------------------------------------------------------

class Recorder:
    def __init__(self, targets):
        self.targets, self.seen, self.done = targets, [], []

    def decide(self, i, equity, position):
        self.seen.append((i, equity, position))
        return self.targets[i]

    def executed(self, i, target):
        self.done.append(i)


def test_engine_asks_the_decider_with_the_state_at_the_decision_close():
    bars = _bars(200)
    targets = np.where(np.arange(200) % 50 < 25, 1.0, 0.0)
    rec = Recorder(targets)
    res = run_backtest(bars, targets, EngineConfig(), decider=rec)
    eq = res.equity
    for i, equity, position in rec.seen[1:]:
        assert equity == pytest.approx(eq["equity"][i]) and position == pytest.approx(eq["qty"][i] * eq["close"][i])
    assert sorted(rec.done) == sorted(int(b) - 1 for b in res.fills["bar_index"])
    plain = run_backtest(bars, targets, EngineConfig())
    assert plain.equity.equals(res.equity)


def test_decider_requires_zero_delay():
    bars = _bars(50)
    with pytest.raises(ValueError, match="delay"):
        run_backtest(bars, np.ones(50), EngineConfig(execution_delay_bars=1), decider=Recorder(np.ones(50)))


def _fixed_blend(target):
    n = len(target)
    return Blend({}, {}, np.asarray(target, float), np.ones(n), np.asarray(target, float))


def test_run_portfolio_respects_the_deadband():
    bars = _bars(200)
    v = _version([_member("a", "f")])
    calm = run_portfolio(v, bars, bl=_fixed_blend(np.where(np.arange(200) % 2, 0.55, 0.50)))
    assert len(calm.result.fills) == 1
    busy = run_portfolio(v, bars, bl=_fixed_blend(np.where(np.arange(200) % 2, 0.6, 0.4)))
    assert len(busy.result.fills) == 199  # every decision bar
    log = calm.log
    assert len(log) == 199 and log["executed"].sum() == 1 and log["pre_state"][0]["last_target"] == 0.0


def test_ladder_liquidates_a_crash_in_the_backtest():
    bars = _bars(600, drift=-0.002, vol=0.002)
    run = run_portfolio(_version([_member("a", "f")]), bars, bl=_fixed_blend(np.ones(600)))
    assert run.state.level == "liquidated" and run.result.equity["qty"].iloc[-1] == 0.0
    assert set(run.log["level"]) == {"full", "halved", "liquidated"}
    first_halved = run.log.index[run.log["level"] == "halved"][0]
    assert run.log["order_target"][first_halved] == pytest.approx(0.5)
    assert pd.isna(run.log["order_target"].iloc[-1])
