"""Paper trading of portfolio versions (design 5.9): replay against the backtest, restart, catch-up, resume, fleet
and approval."""

import json
import math
import os
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from quant_research.backtest.engine import CostConfig, EngineConfig
from quant_research.data.bars import to_epoch
from quant_research.data.validate import validate_bars
from quant_research.paper.risk import RiskLimits
from quant_research.paper.runner import make_trader, run_replay
from quant_research.paper.trader import PaperConfig
from quant_research.paths import get_paths
from quant_research.portfolio.backtest import run_portfolio
from quant_research.portfolio.version import Member, Overlay, PortfolioVersion, VersionError, VersionStore
from tests.conftest import HOUR, T0, make_raw
from tests.test_paper import PRODUCT

COSTS = CostConfig(fee_rate=0.001, slippage_bps=2)


def _bars(n=400, seed=3, drift=0.0002, vol=0.01):
    bars, _ = validate_bars(make_raw(n, seed=seed, drift=drift, vol=vol), "coinbase", "BTC-USD", "ONE_HOUR", T0,
                            T0 + n * HOUR)
    return bars


def _version(members=None, **kw):
    members = members or (Member(entry_id="a", family="trend", strategy_ref="template:sma_cross",
                                 parameters={"fast": 5, "slow": 20}),
                          Member(entry_id="b", family="hold", strategy_ref="template:buy_and_hold"))
    base = {"product": "BTC-USD", "dataset_id": "ds", "split_id": "split-x", "members": tuple(members),
            "member_cap": 1.0, "engine_config": EngineConfig(costs=COSTS)}
    return PortfolioVersion(**{**base, **kw})


def _cfg(version, **kw):
    vid = version.version_id()
    base = {"name": "pf", "instrument": "BTC-USD", "granularity": "ONE_HOUR", "strategy_ref": f"portfolio:{vid}",
            "parameters": {"version_id": vid}, "fee_rate": Decimal("0.001"), "slippage_bps": Decimal("2"),
            "buy_cash_buffer": 0.0, "portfolio": version.model_dump(mode="json"),
            "risk": RiskLimits(max_order_notional=1e9, max_orders_per_hour=100, max_daily_loss_fraction=0.99)}
    return PaperConfig(**{**base, **kw})


def _order_targets(trader):
    return [d["portfolio"]["order_target"] for d in trader.ledger.decisions()]


def test_identity_hash_of_strategy_configs_is_unchanged():
    cfg = PaperConfig(name="t", instrument="BTC-USD", granularity="ONE_HOUR", strategy_ref="template:sma_cross",
                      parameters={"fast": 5, "slow": 20})
    assert cfg.identity_hash() == "412070c301e5"


def test_portfolio_replay_matches_run_portfolio(tmp_path):
    bars, v = _bars(), _version()
    trader = run_replay(_cfg(v), bars, tmp_path / "p", PRODUCT)
    assert type(trader).__name__ == "PortfolioTrader"
    run = run_portfolio(v, bars)
    engine = [None if math.isnan(t) else t for t in run.log["order_target"]]
    paper = _order_targets(trader)
    assert len(paper) == len(bars) and len(engine) == len(bars) - 1
    assert paper[:-1] == pytest.approx(engine, nan_ok=True)
    fills = trader.ledger.fills()
    bt = run.result.fills
    assert len(fills) == len(bt) > 4
    for ev, (_, f) in zip(fills, bt.iterrows(), strict=True):
        assert float(ev.fill_qty) == pytest.approx(f["qty"], rel=1e-6, abs=1e-8)
    final = Decimal(str(bars["close"].iloc[-1]))
    assert float(trader.equity(final)) == pytest.approx(run.result.equity["equity"].iloc[-1], rel=1e-4)
    d = trader.ledger.decisions()[-2]
    assert set(d["portfolio"]) >= {"blend", "pre_overlay", "overlay", "family_targets", "level", "yellow", "peak",
                                   "pre_state", "order_target", "version_id"}
    assert trader.ledger.get("portfolio_state")["last_target"] == run.state.last_target


def test_decider_state_survives_a_restart(tmp_path):
    bars, v = _bars(300), _version()
    whole = run_replay(_cfg(v), bars, tmp_path / "whole", PRODUCT)
    decisions = whole.ledger.decisions()
    cut = next(k for k in range(150, 300) if decisions[k - 1]["intent"] is None)
    first = run_replay(_cfg(v), bars.iloc[:cut], tmp_path / "split", PRODUCT)
    first.ledger.close()
    second = run_replay(_cfg(v), bars, tmp_path / "split", PRODUCT, warmup_bars=cut)
    assert second.decider.state.to_dict() == whole.decider.state.to_dict()
    assert _order_targets(second) == _order_targets(whole)


def _crash_trader(workdir):
    bars = _bars(600, drift=-0.002, vol=0.002)
    v = _version((Member(entry_id="b", family="hold", strategy_ref="template:buy_and_hold"),))
    clock = {"now": 0.0}
    trader = make_trader(_cfg(v), workdir, PRODUCT, clock=lambda: clock["now"])
    rows = bars.to_dict("records")
    return trader, rows, clock


def _deliver(trader, clock, bar):
    clock["now"] = float(to_epoch(pd.Series([bar["bar_open_time"]]))[0])
    trader.process_market(Decimal(str(bar["open"])), clock["now"])
    clock["now"] = float(to_epoch(pd.Series([bar["available_at"]]))[0]) + 1
    return trader.on_bar(bar)


def test_catch_up_bars_update_the_ladder_without_trading(tmp_path):
    trader, rows, clock = _crash_trader(tmp_path / "c")
    _deliver(trader, clock, rows[0])
    _deliver(trader, clock, rows[1])
    assert trader.balances()["BTC"] > 0
    n_orders = len(trader.orders)
    for bar in rows[2:400]:
        trader.ingest(bar)
    assert len(trader.orders) == n_orders
    state = trader.ledger.get("portfolio_state")
    assert state["level"] == "liquidated" and state["pending"] and state["drawdown"] <= -0.30
    rec = _deliver(trader, clock, rows[400])
    assert rec["portfolio"]["order_target"] == 0.0 and rec["intent"]["side"] == "sell"
    assert not trader.ledger.get("portfolio_state")["pending"]


REVIEW_REF = "docs/reviews/2026-11-20-btc-liquidation.md"
REVIEW = ("The cause of the drawdown was a four-week decline that every sleeve held through. The thesis still "
          "holds: the trend sleeve exited late but in line with its dev behaviour. Dev covered a comparable regime "
          "(2022), so the version resumes unchanged.")


def test_liquidated_portfolio_waits_for_resume(tmp_path):
    from quant_research.portfolio.paper import request_resume

    paths = get_paths(tmp_path / "home")
    trader, rows, clock = _crash_trader(paths.paper / "c")
    for bar in rows[:420]:
        _deliver(trader, clock, bar)
    assert trader.decider.state.level == "liquidated" and trader.balances()["BTC"] == 0
    for bar in rows[420:440]:
        rec = _deliver(trader, clock, bar)
        assert rec["intent"] is None and rec["portfolio"]["level"] == "liquidated"
    liquidated_at = trader.ledger.get("portfolio_liquidated_at")
    assert liquidated_at is not None and trader.write_health()["portfolio"]["liquidated_at"] == liquidated_at
    later = liquidated_at + 31 * 86_400
    with pytest.raises(VersionError, match="no paper ledger"):
        request_resume(paths, "other", "go", REVIEW_REF, REVIEW, now=later)
    with pytest.raises(VersionError, match="note"):
        request_resume(paths, "c", " ", REVIEW_REF, REVIEW, now=later)
    with pytest.raises(VersionError, match="docs/reviews"):
        request_resume(paths, "c", "go", "notes/drawdown.md", REVIEW, now=later)
    with pytest.raises(VersionError, match="shorter than 200"):
        request_resume(paths, "c", "go", REVIEW_REF, "fine", now=later)
    with pytest.raises(VersionError, match="less than 30 days"):
        request_resume(paths, "c", "go", REVIEW_REF, REVIEW, now=liquidated_at + 29 * 86_400)
    request_resume(paths, "c", "tampered", REVIEW_REF, REVIEW, now=later)
    f = paths.paper / "c" / "resume.json"
    f.write_text(f.read_text().replace("cause", "c4use"))
    rec = _deliver(trader, clock, rows[440])
    assert rec["portfolio"]["level"] == "liquidated" and not f.exists()
    assert "portfolio_resume_rejected" in (paths.paper / "c" / "events.jsonl").read_text()
    request_resume(paths, "c", "drawdown reviewed; resume", REVIEW_REF, REVIEW, now=later)
    assert trader.write_health()["portfolio"]["resume_pending"]
    rec = _deliver(trader, clock, rows[441])
    assert rec["portfolio"]["level"] == "full" and rec["portfolio"]["peak"] == pytest.approx(float(rec["equity"]))
    assert rec["intent"]["side"] == "buy" and trader.ledger.get("portfolio_liquidated_at") is None
    events = (paths.paper / "c" / "events.jsonl").read_text()
    assert "portfolio_resumed" in events and not f.exists()
    applied = json.loads(next((paths.paper / "c").glob("resume.applied-*.json")).read_text())
    assert applied["review"] == REVIEW and applied["review_ref"] == REVIEW_REF
    with pytest.raises(VersionError, match="not liquidated"):
        request_resume(paths, "c", "again", REVIEW_REF, REVIEW, now=later)


# -- order completion ------------------------------------------------------------------------------------------

HOLD = (Member(entry_id="b", family="hold", strategy_ref="template:buy_and_hold"),)


def _capped_cfg(v, cap=3000):
    return _cfg(v, risk=RiskLimits(max_order_notional=cap, max_orders_per_hour=100, max_daily_loss_fraction=0.99))


def _fraction(trader, price):
    price = Decimal(str(price))
    return float(trader.balances()["BTC"] * price / trader.equity(price))


def test_capped_orders_keep_the_target_open_until_filled(tmp_path):
    bars, v = _bars(20), _version(HOLD)
    trader = run_replay(_capped_cfg(v), bars, tmp_path / "p", PRODUCT)
    buys = [d for d in trader.ledger.decisions() if d["intent"]]
    assert len(buys) == 4 and all(d["portfolio"]["order_target"] == 1.0 for d in buys)
    assert [d["portfolio"]["pre_state"]["open_target"] for d in buys] == [None, 1.0, 1.0, 1.0]
    assert trader.decider.state.last_target == 1.0 and trader.decider.state.open_target is None
    assert _fraction(trader, bars["close"].iloc[-1]) > 0.99
    assert "portfolio_target_incomplete" in (tmp_path / "p" / "events.jsonl").read_text()


def test_target_is_reached_after_the_account_outgrows_the_cap(tmp_path):
    close = np.array([100, 101, 102, 200, 300, 290, 280, 270, 260, 250, 251, 252, 253, 254, 255, 256], float)
    bars = _bars(len(close))
    bars["close"] = close
    bars["open"] = np.r_[close[0], close[:-1]]
    bars["high"] = np.maximum(bars.open, bars.close)
    bars["low"] = np.minimum(bars.open, bars.close)
    v = _version((Member(entry_id="a", family="trend", strategy_ref="template:sma_cross",
                         parameters={"fast": 1, "slow": 2}),),
                 engine_config=EngineConfig(costs=CostConfig(fee_rate=0, slippage_bps=0)))
    trader = run_replay(_capped_cfg(v, cap=10000), bars, tmp_path / "g", PRODUCT)
    assert trader.decider.state.last_target == 1.0 and trader.decider.state.open_target is None
    assert _fraction(trader, close[-1]) > 0.99


def test_an_order_lost_at_the_broker_is_reissued(tmp_path):
    from quant_research.paper.broker import BrokerTimeout

    bars, v = _bars(20), _version(HOLD)
    clock = {"now": 0.0}
    trader = make_trader(_cfg(v), tmp_path / "u", PRODUCT, clock=lambda: clock["now"])
    real_submit, real_query = trader.broker.submit, trader.broker.query

    def lost(intent):
        raise BrokerTimeout("no answer")

    trader.broker.submit, trader.broker.query = lost, lambda cid: None
    rows = bars.to_dict("records")
    _deliver(trader, clock, rows[0])
    assert trader.decider.state.open_target == 1.0 and trader.decider.state.last_target == 0.0
    trader.broker.submit, trader.broker.query = real_submit, real_query
    for bar in rows[1:4]:
        trader.broker.query = lambda cid: None
        _deliver(trader, clock, bar)
        trader.broker.query = real_query
    for bar in rows[4:8]:
        _deliver(trader, clock, bar)
    assert trader.decider.state.last_target == 1.0 and _fraction(trader, rows[7]["close"]) > 0.99


def test_open_order_survives_a_restart(tmp_path):
    bars, v = _bars(20), _version(HOLD)
    first = run_replay(_capped_cfg(v), bars.iloc[:2], tmp_path / "split", PRODUCT)
    assert first.ledger.get("portfolio_open_order")["capped"]
    assert first.decider.state.open_target == 1.0 and first.decider.state.last_target == 0.0
    first.ledger.close()
    second = run_replay(_capped_cfg(v), bars, tmp_path / "split", PRODUCT, warmup_bars=2)
    assert second.decider.state.last_target == 1.0 and second.decider.state.open_target is None
    assert _fraction(second, bars["close"].iloc[-1]) > 0.99


# -- fleet and approval ----------------------------------------------------------------------------------------

def test_fleet_attaches_a_portfolio_entry(tmp_path):
    from quant_research.experiments.store import ExperimentStore
    from quant_research.paper.fleet import PaperFleet, Roster, save_roster
    from tests.test_fleet import NOW0, FakeCoinbase, _entry

    v = _version()
    t = {"now": float(NOW0)}
    paths = get_paths(tmp_path / "home")
    store = ExperimentStore(paths.db)
    vid = VersionStore(store).put(v)
    store.close()
    entry = _entry("portfolio-btc-usd", ref=f"portfolio:{vid}", parameters={"version_id": vid},
                   source="portfolio", portfolio_version=vid, initial_cash=20_000.0)
    roster = paths.desk / "roster.json"
    save_roster(roster, Roster(fee_rate=0.001, slippage_bps=2, risk=RiskLimits(max_order_notional=5_000),
                               entries=[entry, _entry("btc-sma")]))
    fleet = PaperFleet(paths, roster, FakeCoinbase(lambda: t["now"]), clock=lambda: t["now"], seed_bars=500)
    fleet.step()
    for _ in range(3):
        t["now"] += HOUR
        fleet.step()
    assert not fleet.errors
    pf = fleet.traders["portfolio-btc-usd"]
    assert type(pf).__name__ == "PortfolioTrader" and pf.version_id == vid
    assert pf.cfg.risk.max_order_notional == 20_000.0
    assert fleet.traders["btc-sma"].cfg.risk.max_order_notional == 5_000
    assert len(pf.ledger.decisions()) == 3
    other = _version(band=0.05)
    store = ExperimentStore(paths.db)
    vid2 = VersionStore(store).put(other)
    store.close()
    r = Roster.model_validate_json(roster.read_text())
    r.entries[0] = r.entries[0].model_copy(update={"portfolio_version": vid2, "strategy_ref": f"portfolio:{vid2}",
                                                   "parameters": {"version_id": vid2}})
    save_roster(roster, r)
    os.utime(roster, (t["now"] + 5, t["now"] + 5))
    fleet.step()
    assert fleet.errors["portfolio-btc-usd"].startswith("config change refused")
    shadow = entry.model_copy(update={"name": "shadow-btc-usd", "shadow": True})
    plain = entry.model_copy(update={"name": "shadow-btc-usd"})
    assert fleet._config(shadow).identity_hash() == fleet._config(plain).identity_hash()


def test_shadow_runs_an_unapproved_version_without_changing_it(paths, store, dataset, monkeypatch):
    from quant_research.library.store import LibraryStore
    from quant_research.paper.fleet import load_roster
    from quant_research.portfolio import evaluate
    from quant_research.portfolio.paper import start_shadow
    from quant_research.portfolio.seed import define_seed
    from tests.test_portfolio import _desk

    _desk(paths, store, dataset)
    vid = define_seed(paths, store, "BTC-USD")
    with pytest.raises(VersionError, match="is draft"):
        start_shadow(paths, store, vid, "too early")
    monkeypatch.setitem(evaluate.SEED_STANDARD, "min_sharpe_daily", 100.0)
    evaluate.validate(paths, store, vid)
    vs = VersionStore(store)
    assert vs.get(vid)["state"] == "failed_validation"
    with pytest.raises(VersionError, match="needs a note"):
        start_shadow(paths, store, vid, " ")
    out = start_shadow(paths, store, vid, "forward evidence")
    assert out["roster_name"] == "shadow-btc-usd" and vs.get(vid)["state"] == "failed_validation"
    entry = next(e for e in load_roster(paths.desk / "roster.json").entries if e.name == "shadow-btc-usd")
    eng = vs.version(vid).engine_config
    assert entry.shadow and entry.source == "portfolio" and entry.portfolio_version == vid
    assert entry.initial_cash == eng.initial_cash and entry.fee_rate == eng.costs.fee_rate
    event = vs.events(vid)[-1]
    assert event["from_state"] == event["to_state"] == "failed_validation"
    assert event["actor"] == "human" and event["note"] == "forward evidence"
    lib = LibraryStore(paths.db)
    assert lib.entry("btc-tsmom-1w")["status"] == "candidate"
    lib.close()
    with pytest.raises(VersionError, match="already has shadow-btc-usd"):
        start_shadow(paths, store, vid, "twice")


def test_approve_adds_the_roster_entry(paths, store, dataset, monkeypatch):
    from quant_research.library.store import LibraryStore
    from quant_research.paper.fleet import load_roster
    from quant_research.portfolio.paper import approve
    from tests.test_portfolio import _desk, _lenient

    _desk(paths, store, dataset)
    _lenient(monkeypatch)
    from quant_research.portfolio.evaluate import validate
    from quant_research.portfolio.seed import define_seed

    vid = define_seed(paths, store, "BTC-USD")
    with pytest.raises(VersionError, match="passed the holdout"):
        approve(paths, store, vid, "too early")
    validate(paths, store, vid)
    vs = VersionStore(store)
    vs.transition(vid, "frozen", "human", note="t")
    vs.transition(vid, "final_evaluated", "evaluator", {"forced": True})
    out = approve(paths, store, vid, "go to paper")
    assert out["roster_name"] == "portfolio-btc-usd" and vs.get(vid)["state"] == "paper"
    entry = next(e for e in load_roster(paths.desk / "roster.json").entries if e.name == "portfolio-btc-usd")
    assert entry.source == "portfolio" and entry.portfolio_version == vid and entry.strategy_ref == f"portfolio:{vid}"
    lib = LibraryStore(paths.db)
    assert lib.entry("btc-tsmom-1w")["status"] == "member" and lib.entry("btc-voltarget-1m")["status"] == "member"
    assert vid in lib.events("btc-tsmom-1w")[-1]["note"]
    lib.close()
    assert len(out["library_members"]) == 12
    with pytest.raises(VersionError):
        approve(paths, store, vid, "twice")


# -- parity ----------------------------------------------------------------------------------------------------

def _parity(tmp_path, written, checked, tamper=None, cap=1e9):
    from quant_research.paper.fleet import Roster, RosterEntry
    from quant_research.paper.parity import portfolio_parity

    bars = _bars()
    trader = run_replay(_capped_cfg(written, cap), bars, tmp_path / "p", PRODUCT)
    if tamper:
        tamper(trader.ledger)
    vid = checked.version_id()
    entry = RosterEntry(name="pf", product="BTC-USD", strategy_ref=f"portfolio:{vid}", source="portfolio",
                        portfolio_version=vid, parameters={"version_id": vid}, fee_rate=0.001, slippage_bps=2)
    roster = Roster(fee_rate=0.001, slippage_bps=2, risk=RiskLimits(max_order_notional=cap))
    return portfolio_parity(entry, roster, trader.ledger, bars, checked, None)


OVERLAY = Overlay(entry_id="o", strategy_ref="template:vol_target", parameters={"window": 48, "target_vol": 0.4})


def test_portfolio_parity_is_clean_on_a_replay(tmp_path):
    v = _version(overlay=OVERLAY)
    out = _parity(tmp_path, v, v)
    assert out["checked"] == 400 and out["fills"] > 4
    assert not out["target_mismatches"] and not out["order_mismatches"] and not out["size_mismatches"]
    assert abs(out["path"]["equity_gap_bps"]) < 1 and abs(out["fill_vs_next_open_bps"]) < 0.01


def test_portfolio_parity_is_clean_with_capped_orders(tmp_path):
    v = _version(overlay=OVERLAY)
    out = _parity(tmp_path, v, v, cap=3000)
    assert out["allowed"].get("order_cap", 0) > 0
    assert not out["target_mismatches"] and not out["order_mismatches"] and not out["size_mismatches"]


def test_portfolio_parity_finds_a_changed_band(tmp_path):
    out = _parity(tmp_path, _version(overlay=OVERLAY, band=0.05), _version(overlay=OVERLAY, band=0.10))
    assert out["order_mismatches"] and not out["target_mismatches"]


def test_portfolio_parity_finds_a_wrong_blend(tmp_path):
    import json as _json

    def tamper(ledger):
        rid, data = ledger._conn.execute("SELECT id, data FROM decisions ORDER BY id LIMIT 1 OFFSET 100").fetchone()
        d = _json.loads(data)
        d["portfolio"]["blend"] += 0.2
        ledger._conn.execute("UPDATE decisions SET data=? WHERE id=?", (_json.dumps(d), rid))

    v = _version()
    out = _parity(tmp_path, v, v, tamper)
    assert len(out["target_mismatches"]) == 1
