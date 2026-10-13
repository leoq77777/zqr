"""Offline review probes, not acceptance tests; reproduce current defects on synthetic data.

Run from the repository root:
  .venv/bin/python docs/reviews/2026-10-13/reproduce_findings.py
No model calls, network requests, trading, or production-state writes.
"""

# ruff: noqa: E402 -- make the checkout's existing synthetic test fixtures importable.

from __future__ import annotations

import json
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from quant_research.backtest.engine import CostConfig, EngineConfig, run_backtest
from quant_research.data.snapshot import freeze_dataset
from quant_research.data.validate import validate_bars
from quant_research.experiments.runner import ExperimentRequest
from quant_research.experiments.store import ExperimentStore
from quant_research.paper.feeds import CoinbaseCandlePoller
from quant_research.paper.fleet import PaperFleet, Roster, RosterEntry, load_roster, save_roster
from quant_research.paper.runner import make_trader, run_replay
from quant_research.paper.trader import target_fraction
from quant_research.paths import get_paths
from quant_research.portfolio.policy import PortfolioPolicy
from quant_research.strategy.spec import StrategySpec
from quant_research.validation.holdout import run_holdout
from quant_research.validation.splits import create_split
from tests.conftest import HOUR, T0, make_raw
from tests.test_desk import EASY_HOLDOUT, _authorize, _validated
from tests.test_fleet import NOW0, FakeCoinbase
from tests.test_paper import PRODUCT, _cfg


def probe_band(base: Path) -> dict:
    raw = make_raw(24)
    px = np.linspace(100, 110, len(raw))
    for col in ("open", "high", "low", "close"):
        raw[col] = px
    bars, _ = validate_bars(raw, "coinbase", "BTC-USD", "ONE_HOUR", T0, T0 + 24 * HOUR)
    policy = PortfolioPolicy(asset_cap=0.5, band=0.1)
    cfg = _cfg(strategy_ref="template:buy_and_hold", parameters={}, policy=policy,
               fee_rate=Decimal(0), slippage_bps=Decimal(0), buy_cash_buffer=0)
    trader = run_replay(cfg, bars, base / "band", PRODUCT)
    bt = run_backtest(bars, np.full(len(bars), 0.5),
                      policy.engine_config(EngineConfig(costs=CostConfig(fee_rate=0, slippage_bps=0))))
    out = {"backtest_fills": len(bt.fills), "paper_fills": len(trader.ledger.fills()), "band": policy.band}
    trader.ledger.close()
    assert out["backtest_fills"] == 1 and out["paper_fills"] > 1, out
    return out


def probe_stale(base: Path) -> dict:
    now = T0 + 10 * HOUR

    class OldCandles:
        def candles(self, *_):
            return SimpleNamespace(body={"candles": [{"start": now - 5 * HOUR, "open": 100,
                "high": 100, "low": 100, "close": 100, "volume": 10}]})

    poller = CoinbaseCandlePoller(OldCandles(), "BTC-USD", "ONE_HOUR", clock=lambda: now)
    bar = poller.poll()[0]
    trader = make_trader(_cfg(strategy_ref="template:buy_and_hold", parameters={}), base / "stale",
                         PRODUCT, clock=lambda: now)
    decision = trader.on_bar(bar)
    out = {"bar_age_seconds": now - bar["bar_close_time"].timestamp(),
           "reported_data_age_seconds": trader.write_health()["data_age_seconds"],
           "approved": decision["decision"]["approved"], "order_created": decision["intent"] is not None}
    trader.ledger.close()
    assert out["bar_age_seconds"] > 7200 and out["approved"] and out["order_created"], out
    return out


def probe_gap() -> dict:
    clock = {"now": float(NOW0)}
    poller = CoinbaseCandlePoller(FakeCoinbase(lambda: clock["now"]), "BTC-USD", "ONE_HOUR",
                                clock=lambda: clock["now"])
    poller.history(20)
    last = poller.last_open
    clock["now"] += 10 * HOUR
    bars = poller.poll()
    out = {"returned_bars": len(bars),
           "missing_bars_before_first_returned": int((bars[0]["bar_open_time"].timestamp() - last) / HOUR) - 1}
    assert out["missing_bars_before_first_returned"] == 5, out
    return out


def new_fleet(base: Path, client=None):
    paths = get_paths(base)
    rp = paths.desk / "roster.json"
    save_roster(rp, Roster(entries=[RosterEntry(name="btc", product="BTC-USD",
                                               strategy_ref="template:buy_and_hold")]))
    client = client or FakeCoinbase(lambda: NOW0)
    return PaperFleet(paths, rp, client, clock=lambda: NOW0), rp


def probe_reload(base: Path) -> dict:
    fleet, rp = new_fleet(base / "reload")
    fleet.step()
    old = rp.stat().st_mtime
    roster = load_roster(rp)
    roster.risk.max_order_notional = 100
    save_roster(rp, roster)
    os.utime(rp, (old + 2, old + 2))
    fleet.step()
    out = {"roster_max_order_notional": fleet.roster.risk.max_order_notional,
           "running_max_order_notional": fleet.traders["btc"].cfg.risk.max_order_notional}
    for trader in fleet.traders.values():
        trader.ledger.close()
    assert out == {"roster_max_order_notional": 100, "running_max_order_notional": 5000}, out
    return out


def probe_attach_retry(base: Path) -> dict:
    class TransientClient(FakeCoinbase):
        attempts = 0

        def candles(self, *args):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("synthetic one-off feed outage")
            return super().candles(*args)

    client = TransientClient(lambda: NOW0)
    fleet, _ = new_fleet(base / "retry", client)
    fleet.step()
    fleet.step()
    out = {"fetch_attempts_after_two_steps": client.attempts, "active_strategies": len(fleet.traders)}
    assert out == {"fetch_attempts_after_two_steps": 1, "active_strategies": 0}, out
    return out


def probe_holdout(base: Path) -> dict:
    paths = get_paths(base / "holdout")
    raw = make_raw(120)
    bars, rep = validate_bars(raw, "coinbase", "BTC-USD", "ONE_HOUR", T0, T0 + 120 * HOUR)
    manifest = freeze_dataset(paths, bars, rep)
    split = create_split(paths, manifest["dataset_id"])
    store = ExperimentStore(paths.db)
    spec = StrategySpec(strategy_ref="template:buy_and_hold").normalized()
    store.freeze_candidate(spec.candidate_hash(), spec.strategy_ref, spec.parameters, "review fixture")
    req = ExperimentRequest(strategy_ref=spec.strategy_ref, dataset_id=manifest["dataset_id"],
                            split_id=split.split_id)
    results = [run_holdout(paths, store, req)[0].status for _ in range(11)]
    out = {"viewings": len(store.holdout_runs(split.split_id)), "eleventh_status": results[-1],
           "mandate_required": False}
    store.close()
    assert out["viewings"] == 11 and out["eleventh_status"] == "duplicate", out
    return out


def probe_history() -> dict:
    raw = make_raw(4000)
    for col in ("open", "high", "low", "close"):
        raw[col] = 100.0
        raw.loc[100:, col] = 110.0
    bars, _ = validate_bars(raw, "coinbase", "BTC-USD", "ONE_HOUR", T0, T0 + 4000 * HOUR)
    spec = StrategySpec(strategy_ref="template:breakout", parameters={"entry": 48, "exit": 24})
    full = target_fraction(spec, PortfolioPolicy(), bars, "ONE_HOUR")
    truncated = target_fraction(spec, PortfolioPolicy(), bars.tail(3000), "ONE_HOUR")
    out = {"full_history_target": full, "paper_3000_bar_target": truncated}
    assert out == {"full_history_target": 1.0, "paper_3000_bar_target": 0.0}, out
    return out


def probe_promotion(base: Path) -> dict:
    from quant_research.desk import promotion

    paths = get_paths(base / "promotion")
    raw = make_raw(300)
    bars, rep = validate_bars(raw, "coinbase", "BTC-USD", "ONE_HOUR", T0, T0 + 300 * HOUR)
    manifest = freeze_dataset(paths, bars, rep)
    dataset = (manifest, create_split(paths, manifest["dataset_id"]))
    store = ExperimentStore(paths.db)
    engine = EngineConfig(initial_cash=20000, costs=CostConfig(fee_rate=0.001, slippage_bps=2))
    first = _validated(paths, store, dataset, holdout=EASY_HOLDOUT, engine_config=engine)
    _authorize(paths, max_paper_strategies=1)
    assert promotion.promote(paths, store, first)["stopped"] == "done"
    roster = load_roster(promotion.roster_path(paths))
    drift = {"research_initial_cash": engine.initial_cash, "paper_initial_cash": roster.entries[0].initial_cash,
             "research_fee_rate": engine.costs.fee_rate, "paper_fee_rate": roster.fee_rate}
    second = _validated(paths, store, dataset, template="momentum", params={"lookback": 24},
                        holdout=EASY_HOLDOUT)
    stopped = promotion.promote(paths, store, second)
    roster.entries[0].enabled = False
    save_roster(promotion.roster_path(paths), roster)
    resumed = promotion.promote(paths, store, second)
    out = {"config_drift": drift, "state_when_cap_hit": stopped["state"],
           "retry_after_slot_freed": resumed["stopped"]}
    store.close()
    assert drift["research_fee_rate"] != drift["paper_fee_rate"], out
    assert stopped["state"] == "final_evaluated" and resumed["stopped"].startswith("not validated"), out
    return out


def main():
    with tempfile.TemporaryDirectory(prefix="zqr-review-") as tmp:
        base = Path(tmp)
        result = {"band_not_enforced": probe_band(base), "stale_bar_accepted": probe_stale(base),
                  "gap_not_backfilled": probe_gap(), "risk_reload_ignored": probe_reload(base),
                  "attach_not_retried": probe_attach_retry(base), "legacy_holdout_uncapped": probe_holdout(base),
                  "history_state_lost": probe_history(), "promotion": probe_promotion(base)}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
