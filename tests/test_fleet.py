import json
import os
import sqlite3
import zlib
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from quant_research.data.bars import to_epoch
from quant_research.desk.presets import preset_roster
from quant_research.paper.barstore import BarStore
from quant_research.paper.fleet import PaperFleet, Roster, RosterEntry, add_entry, load_roster, save_roster
from quant_research.paper.ledger import Ledger
from quant_research.paper.risk import RiskLimits
from quant_research.paper.trader import target_fraction
from quant_research.paths import get_paths
from quant_research.portfolio.policy import PortfolioPolicy
from quant_research.strategy.spec import StrategySpec

HOUR = 3600
NOW0 = 1_760_000_000 - 1_760_000_000 % HOUR + 600


class FakeCoinbase:
    """Deterministic hourly prices; only bars that have closed by `clock()` are returned."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = {"candles": 0, "ticker": 0}

    @staticmethod
    def price(product, t):
        rng = np.random.default_rng(zlib.crc32(f"{product}:{t // HOUR}".encode()))
        base = 100.0 if product.startswith("BTC") else 50.0
        return base * (1 + 0.2 * np.sin(t / (HOUR * 40))) * (1 + 0.003 * rng.standard_normal())

    def candles(self, product, start, end, granularity):
        self.calls["candles"] += 1
        out = []
        for s in range(start, end + 1, HOUR):
            if s + HOUR > self.clock():
                break
            o, c = self.price(product, s), self.price(product, s + HOUR)
            out.append({"start": str(s), "open": o, "high": max(o, c) * 1.001, "low": min(o, c) * 0.999,
                        "close": c, "volume": 5.0})
        return SimpleNamespace(body={"candles": out[::-1]})

    def ticker(self, product):
        self.calls["ticker"] += 1
        p = self.price(product, self.clock())
        return SimpleNamespace(body={"best_bid": str(round(p * 0.9999, 2)), "best_ask": str(round(p * 1.0001, 2))})

    def product(self, product):
        return SimpleNamespace(body={"product_id": product, "base_increment": "0.00000001", "quote_increment": "0.01",
                                     "base_min_size": "0.00000001", "base_max_size": "1000000",
                                     "quote_min_size": "1"})


def _entry(name, product="BTC-USD", ref="template:sma_cross", **kw):
    params = kw.pop("parameters", {"fast": 5, "slow": 20} if ref == "template:sma_cross" else {})
    return RosterEntry(name=name, product=product, strategy_ref=ref, parameters=params, **kw)


def _setup(tmp_path, entries):
    t = {"now": float(NOW0)}
    paths = get_paths(tmp_path / "home")
    roster = paths.desk / "roster.json"
    save_roster(roster, Roster(fee_rate=0.001, slippage_bps=2,
                               risk=RiskLimits(max_order_notional=1e9, max_orders_per_hour=100,
                                               max_daily_loss_fraction=0.99), entries=entries))
    client = FakeCoinbase(lambda: t["now"])
    return t, paths, roster, client


def _bump(path, t):
    os.utime(path, (t, t))


def test_fleet_runs_many_strategies_on_shared_feeds(tmp_path):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma"), _entry("btc-bh", ref="template:buy_and_hold"),
                                                 _entry("eth-sma", product="ETH-USD")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    health = fleet.step()
    assert set(health["strategies"]) == {"btc-sma", "btc-bh", "eth-sma"}
    assert len(fleet.feeds) == 2  # one feed per product, shared by both BTC strategies
    assert not any(h["stale"] for h in health["strategies"].values())  # warm-up counts as fresh data
    for _ in range(5):
        t["now"] += HOUR
        health = fleet.step()
    assert not health["errors"]
    for name in ("btc-sma", "btc-bh", "eth-sma"):
        ledger = Ledger(paths.paper / name / "paper.db")
        decisions = ledger.decisions()
        assert len(decisions) == 5
        assert len(fleet.traders[name].history) >= 1000
        ledger.close()
    bh = fleet.traders["btc-bh"]
    assert bh.balances()["BTC"] > 0  # buy and hold is invested


def test_recorded_window_reproduces_each_decision(tmp_path):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    for _ in range(3):
        t["now"] += HOUR
        fleet.step()
    ledger = fleet.traders["btc-sma"].ledger
    bars = fleet.feeds[("BTC-USD", "ONE_HOUR")].history
    spec = StrategySpec(strategy_ref="template:sma_cross", parameters={"fast": 5, "slow": 20})
    decisions = ledger.decisions()
    assert len(decisions) == 3
    for d in decisions:
        window = bars[(bars["bar_open_time"] >= d["history_start"]) & (bars["bar_open_time"] <= d["bar_open"])]
        frac = target_fraction(spec, PortfolioPolicy(), window.reset_index(drop=True), "ONE_HOUR")
        assert frac == d["target"]["target_fraction"]


def test_roster_hot_reload_attaches_and_detaches(tmp_path):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma")])
    activated = []
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], on_activate=activated.append,
                       seed_bars=1000)
    fleet.step()
    assert add_entry(roster, _entry("eth-bh", product="ETH-USD", ref="template:buy_and_hold",
                                    source="candidate", candidate_id="cand-x"))
    assert not add_entry(roster, _entry("eth-bh", product="ETH-USD"))
    _bump(roster, NOW0 + 1)
    t["now"] += HOUR
    assert set(fleet.step()["strategies"]) == {"btc-sma", "eth-bh"}
    assert [e.name for e in activated] == ["btc-sma", "eth-bh"]

    r = load_roster(roster)
    r.entries[0].enabled = False
    save_roster(roster, r)
    _bump(roster, NOW0 + 2)
    t["now"] += HOUR
    assert set(fleet.step()["strategies"]) == {"eth-bh"}
    assert (paths.paper / "btc-sma" / "paper.db").exists()  # detached strategies keep their ledger

    roster.write_text("{not json")
    _bump(roster, NOW0 + 3)
    assert set(fleet.step()["strategies"]) == {"eth-bh"}  # a broken roster edit keeps the running set
    events = [json.loads(x)["event"] for x in (paths.paper / "fleet.jsonl").read_text().splitlines()]
    assert "roster_invalid" in events and "strategy_detached" in events


def test_one_failing_strategy_does_not_stop_the_others(tmp_path):
    t, paths, roster, client = _setup(tmp_path, [_entry("good"), _entry("bad"),
                                                 _entry("broken", ref="template:no_such_template")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    assert "broken" in fleet.errors and "broken" not in fleet.traders

    def boom(bar):
        raise RuntimeError("strategy blew up")

    fleet.traders["bad"].on_bar = boom
    t["now"] += HOUR
    health = fleet.step()
    assert "strategy blew up" in health["errors"]["bad"]
    assert len(fleet.traders["good"].ledger.decisions()) == 1


def test_preset_roster_covers_both_products():
    r = preset_roster()
    names = [e.name for e in r.entries]
    assert len(names) == len(set(names)) == 16
    assert {e.product for e in r.entries} == {"BTC-USD", "ETH-USD"}
    assert all(e.strategy_ref.startswith("template:") and e.source == "preset" for e in r.entries)


def test_fleet_catches_up_after_an_outage_and_trades_only_the_latest_bar(tmp_path):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    t["now"] += 10 * HOUR
    fleet.step()
    trader = fleet.traders["btc-sma"]
    opens = to_epoch(trader.history["bar_open_time"])
    assert (np.diff(opens) == HOUR).all()
    assert len(trader.ledger.decisions()) == 1
    store = BarStore.for_feed(paths, "BTC-USD", "ONE_HOUR")
    assert store.missing_opens() == [] and store.last_open() == opens[-1]


def test_a_restart_after_an_outage_trades_the_latest_bar(tmp_path):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    for _ in range(3):
        t["now"] += HOUR
        fleet.step()
    fleet.traders["btc-sma"].ledger.close()
    t["now"] += 6 * HOUR
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    trader = fleet.traders["btc-sma"]
    store = fleet.feeds[("BTC-USD", "ONE_HOUR")].store
    decisions = trader.ledger.decisions()
    assert len(decisions) == 4
    assert to_epoch(pd.to_datetime(pd.Series([decisions[-1]["bar_open"]]), utc=True))[0] == store.last_open()
    assert (to_epoch(trader.history["bar_open_time"]) == to_epoch(store.bars["bar_open_time"])).all()
    assert len(trader.history) == len(store.bars)


def test_a_failed_catch_up_bar_is_retried_on_the_next_step(tmp_path, monkeypatch):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    trader = fleet.traders["btc-sma"]
    ingest, calls = trader.ingest, {"n": 0}

    def flaky(bar):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("one-off ingest failure")
        return ingest(bar)

    monkeypatch.setattr(trader, "ingest", flaky)
    t["now"] += 5 * HOUR
    fleet.step()
    assert "one-off ingest failure" in fleet.errors["btc-sma"]
    t["now"] += HOUR
    fleet.step()
    store = fleet.feeds[("BTC-USD", "ONE_HOUR")].store
    assert len(trader.history) == len(store.bars)
    assert (to_epoch(trader.history["bar_open_time"]) == to_epoch(store.bars["bar_open_time"])).all()
    assert "btc-sma" not in fleet.errors


def test_an_empty_trader_warms_up_from_the_store_instead_of_ingesting_every_bar(tmp_path, monkeypatch):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    trader = fleet.traders["btc-sma"]
    trader.history = pd.DataFrame()
    ingested = []
    monkeypatch.setattr(trader, "ingest", ingested.append)
    t["now"] += HOUR
    fleet.step()
    store = fleet.feeds[("BTC-USD", "ONE_HOUR")].store
    assert not ingested
    assert len(trader.history) == len(store.bars)
    assert len(trader.ledger.decisions()) == 1


def test_risk_edits_reach_running_traders_and_identity_edits_are_refused(tmp_path):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    r = load_roster(roster)
    r.risk.max_order_notional = 100
    save_roster(roster, r)
    _bump(roster, NOW0 + 1)
    fleet.step()
    trader = fleet.traders["btc-sma"]
    assert trader.cfg.risk.max_order_notional == 100 and trader.risk.limits.max_order_notional == 100
    r.entries[0].parameters = {"fast": 6, "slow": 20}
    save_roster(roster, r)
    _bump(roster, NOW0 + 2)
    health = fleet.step()
    assert fleet.traders["btc-sma"].cfg.parameters == {"fast": 5, "slow": 20}
    assert health["errors"]["btc-sma"].startswith("config change refused")
    assert set(health["config_refused"]["btc-sma"]) == {"running", "roster"}


def test_an_identity_edit_is_refused_across_a_restart(tmp_path):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    running = fleet.traders["btc-sma"].cfg.identity_hash()
    fleet.traders["btc-sma"].ledger.close()
    r = load_roster(roster)
    r.entries[0].parameters = {"fast": 6, "slow": 20}
    save_roster(roster, r)
    t["now"] += HOUR
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    health = fleet.step()
    assert "btc-sma" not in fleet.traders
    assert fleet.errors["btc-sma"].startswith("config change refused")
    assert health["config_refused"]["btc-sma"]["running"] == running
    ledger = Ledger(paths.paper / "btc-sma" / "paper.db")
    assert ledger.get("config_hash") == running
    ledger.close()


def test_a_ledger_without_a_config_hash_adopts_the_roster_config(tmp_path):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    ledger = fleet.traders["btc-sma"].ledger
    ledger._conn.execute("DELETE FROM kv WHERE key='config_hash'")
    ledger.close()
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    trader = fleet.traders["btc-sma"]
    assert trader.ledger.get("config_hash") == trader.cfg.identity_hash()


def test_a_failed_attach_closes_the_traders_ledger(tmp_path, monkeypatch):
    import quant_research.paper.fleet as fleet_mod

    made, make_trader = [], fleet_mod.make_trader

    def boom(bars):
        raise RuntimeError("warm-up failed")

    def make(*args, **kw):
        made.append(make_trader(*args, **kw))
        made[-1].warm_up = boom
        return made[-1]

    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    monkeypatch.setattr(fleet_mod, "make_trader", make)
    fleet.step()
    assert "warm-up failed" in fleet.errors["btc-sma"] and len(made) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        made[0].ledger.get("config_hash")


def test_a_detached_strategy_drops_its_error(tmp_path):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma"), _entry("btc-bh", ref="template:buy_and_hold")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    fleet.errors["btc-bh"] = "RuntimeError: stale"
    r = load_roster(roster)
    r.entries[1].enabled = False
    save_roster(roster, r)
    _bump(roster, NOW0 + 1)
    assert "btc-bh" not in fleet.step()["errors"]


def test_a_failed_attach_is_retried_without_a_roster_edit(tmp_path):
    class Flaky(FakeCoinbase):
        attempts = 0

        def candles(self, *args):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("one-off outage")
            return super().candles(*args)

    t, paths, roster, _ = _setup(tmp_path, [_entry("btc-sma")])
    fleet = PaperFleet(paths, roster, Flaky(lambda: t["now"]), clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    assert "btc-sma" in fleet.errors and not fleet.traders
    t["now"] += 30
    fleet.step()
    assert not fleet.traders  # backing off
    t["now"] += 31
    fleet.step()
    assert "btc-sma" in fleet.traders and "btc-sma" not in fleet.errors


def test_entry_costs_override_the_roster_defaults(tmp_path):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma", initial_cash=20_000.0, fee_rate=0.002,
                                                        slippage_bps=3.0, min_trade_notional=5.0)])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    cfg = fleet.traders["btc-sma"].cfg
    assert (cfg.initial_quote, cfg.fee_rate, cfg.slippage_bps, cfg.min_trade_notional) == (
        Decimal("20000"), Decimal("0.002"), Decimal("3.0"), 5.0)
