from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest

from quant_research.backtest.engine import CostConfig, EngineConfig, run_backtest
from quant_research.data.validate import validate_bars
from quant_research.paper.broker import SimulatedBroker
from quant_research.paper.feeds import CoinbaseCandlePoller
from quant_research.paper.models import BrokerEvent, BrokerEventType, OrderIntent, ProductSpec, Side
from quant_research.paper.orders import Order, OrderError, OrderState
from quant_research.paper.risk import RiskLimits
from quant_research.paper.runner import make_trader, run_replay
from quant_research.paper.trader import PaperConfig, target_fraction
from quant_research.portfolio.policy import PortfolioPolicy
from quant_research.strategy.compute import compute_targets
from quant_research.strategy.spec import StrategySpec
from tests.conftest import HOUR, T0, make_raw

PRODUCT = ProductSpec(product_id="BTC-USD", base_currency="BTC", quote_currency="USD",
                      base_increment=Decimal("0.00000001"), quote_increment=Decimal("0.01"),
                      base_min_size=Decimal("0.00000001"), base_max_size=Decimal("1000000"),
                      min_market_funds=Decimal("1"))


def _bars(n=400, seed=3):
    raw = make_raw(n, seed=seed, vol=0.01)
    bars, _ = validate_bars(raw, "coinbase", "BTC-USD", "1h", T0, T0 + n * HOUR)
    return bars


def _cfg(**kw):
    base = {"name": "t", "instrument": "BTC-USD", "granularity": "ONE_HOUR", "strategy_ref": "template:sma_cross",
            "parameters": {"fast": 5, "slow": 20}, "fee_rate": Decimal("0.001"), "slippage_bps": Decimal("2"),
            "risk": RiskLimits(max_order_notional=1e9, max_orders_per_hour=100, max_daily_loss_fraction=0.99)}
    return PaperConfig(**{**base, **kw})


def _intent(cid="c1", size="1"):
    return OrderIntent(client_order_id=cid, instrument="BTC-USD", side=Side.BUY, base_size=Decimal(size),
                       created_at=0, reference_price=Decimal("100"))


def test_order_state_machine_dedupes_and_rejects_overfill():
    o = Order(intent=_intent(size="2"))
    o.transition(OrderState.SUBMITTED)
    fill = BrokerEvent(event_id="e1", client_order_id="c1", type=BrokerEventType.FILL, fill_qty=Decimal("1"),
                       fill_price=Decimal("100"), ts=1)
    assert o.apply(fill) and o.state == OrderState.PARTIALLY_FILLED
    assert not o.apply(fill) and o.filled_qty == 1  # duplicate ignored
    o.apply(fill.model_copy(update={"event_id": "e2"}))
    assert o.state == OrderState.FILLED and o.avg_price == 100
    with pytest.raises(OrderError):
        o.apply(fill.model_copy(update={"event_id": "e3"}))


def test_paper_replay_matches_backtest(tmp_path):
    bars = _bars()
    cfg = _cfg(buy_cash_buffer=0.0)
    trader = run_replay(cfg, bars, tmp_path / "p", PRODUCT, warmup_bars=0)
    spec = StrategySpec(strategy_ref=cfg.strategy_ref, parameters=cfg.parameters)
    targets = compute_targets(spec, bars).targets
    bt = run_backtest(bars, targets, EngineConfig(initial_cash=10_000, costs=CostConfig(fee_rate=0.001,
                                                                                        slippage_bps=2)))
    paper_fills = [ev for ev in trader.ledger.fills()]
    assert len(paper_fills) == len(bt.fills) > 4
    final_px = Decimal(str(bars["close"].iloc[-1]))
    assert float(trader.equity(final_px)) == pytest.approx(bt.equity["equity"].iloc[-1], rel=0.01)
    assert not trader.reconcile()


def test_timeout_after_accept_goes_unknown_then_reconciles_without_resend(tmp_path):
    bars = _bars(120)
    trader = run_replay(_cfg(), bars, tmp_path / "p", PRODUCT, faults=["timeout_after_accept"])
    events = (tmp_path / "p" / "events.jsonl").read_text()
    assert "order_state_unknown" in events
    first = next(iter(trader.orders.values()))
    assert first.state == OrderState.FILLED  # resolved by query, filled exactly once
    assert len({ev.client_order_id for ev in trader.ledger.fills()}) == len(
        [o for o in trader.orders.values() if o.state == OrderState.FILLED])
    assert not trader.reconcile()


def test_timeout_before_accept_is_closed_after_not_found_queries(tmp_path):
    trader = run_replay(_cfg(unknown_query_limit=2), _bars(150), tmp_path / "p", PRODUCT,
                        faults=["timeout_before_accept"])
    first = next(iter(trader.orders.values()))
    assert first.state == OrderState.REJECTED and "not found" in first.reject_reason
    assert any(o.state == OrderState.FILLED for o in trader.orders.values())


def test_duplicate_events_partial_fills_and_rejects(tmp_path):
    trader = run_replay(_cfg(), _bars(200), tmp_path / "p", PRODUCT, faults=["duplicate", "partial", "reject"])
    assert "duplicate_event_ignored" in (tmp_path / "p" / "events.jsonl").read_text()
    states = [o.state for o in trader.orders.values()]
    assert OrderState.REJECTED in states
    assert not trader.reconcile()
    fills_per_order = {}
    for ev in trader.ledger.fills():
        fills_per_order[ev.client_order_id] = fills_per_order.get(ev.client_order_id, 0) + 1
    partial = [cid for cid, n in fills_per_order.items() if n == 2]
    assert partial and trader.orders[partial[0]].state == OrderState.FILLED
    # the duplicated fill was applied once: ledger quantity equals the order size
    dup_order = list(trader.orders.values())[0]
    assert dup_order.filled_qty == dup_order.intent.base_size and fills_per_order[dup_order.intent.client_order_id] == 1


def test_restart_recovers_from_ledger(tmp_path):
    bars = _bars(300)
    cfg = _cfg()
    t1 = run_replay(cfg, bars.iloc[:150], tmp_path / "p", PRODUCT)
    n_orders = len(t1.orders)
    bal1 = t1.balances()
    t1.ledger.close()
    t2 = make_trader(cfg, tmp_path / "p", PRODUCT, clock=lambda: 0)
    assert len(t2.orders) == n_orders and t2.balances() == bal1
    t2.recover()
    assert not t2.reconcile()


def test_stale_data_and_kill_switch_block_new_exposure(tmp_path):
    bars = _bars(100)
    kill = tmp_path / "KILL"
    cfg = _cfg(risk=RiskLimits(max_order_notional=1e9, max_orders_per_hour=100, stale_after_seconds=60,
                               kill_switch_file=str(kill)))
    # replay delay 3600s after availability -> every decision sees stale data -> no orders at all
    trader = run_replay(cfg, bars, tmp_path / "stale", PRODUCT, delay_seconds=3600)
    assert not trader.orders
    assert '"stale": true' in (tmp_path / "stale" / "health.json").read_text()

    kill.write_text("stop")
    trader = run_replay(_cfg(risk=cfg.risk.model_copy(update={"stale_after_seconds": 7200})), bars,
                        tmp_path / "killswitch_run", PRODUCT)
    assert not [o for o in trader.orders.values() if o.intent.side == Side.BUY]


def test_simulated_broker_rejects_duplicate_client_ids():
    b = SimulatedBroker(PRODUCT, {"BTC": Decimal(0), "USD": Decimal(1000)})
    assert b.submit(_intent(size="0.1"))[0].type == BrokerEventType.ACK
    assert b.submit(_intent(size="0.1"))[0].reason == "duplicate client_order_id"
    fills = b.on_market(Decimal("100"), 1)
    assert len(fills) == 1 and b.balances()["BTC"] == Decimal("0.1")


def test_quantization_rounds_down():
    p = PRODUCT.model_copy(update={"base_increment": Decimal("0.001")})
    assert p.quantize_base(Decimal("1.23456")) == Decimal("1.234")
    assert np.isclose(float(p.quantize_base(Decimal("0.0009"))), 0)


def test_paper_honours_the_band_like_the_backtest(tmp_path):
    raw = make_raw(24)
    px = np.linspace(100, 110, len(raw))
    for col in ("open", "high", "low", "close"):
        raw[col] = px
    bars, _ = validate_bars(raw, "coinbase", "BTC-USD", "ONE_HOUR", T0, T0 + 24 * HOUR)
    policy = PortfolioPolicy(asset_cap=0.5, band=0.1)
    cfg = _cfg(strategy_ref="template:buy_and_hold", parameters={}, policy=policy, fee_rate=Decimal(0),
               slippage_bps=Decimal(0), buy_cash_buffer=0)
    trader = run_replay(cfg, bars, tmp_path / "band", PRODUCT)
    bt = run_backtest(bars, np.full(len(bars), 0.5),
                      policy.engine_config(EngineConfig(costs=CostConfig(fee_rate=0, slippage_bps=0))))
    assert len(trader.ledger.fills()) == len(bt.fills) == 1


def test_breakout_position_survives_long_history_and_restart(tmp_path):
    raw = make_raw(4000)
    for col in ("open", "high", "low", "close"):
        raw[col] = 100.0
        raw.loc[100:, col] = 110.0
    bars, _ = validate_bars(raw, "coinbase", "BTC-USD", "ONE_HOUR", T0, T0 + 4000 * HOUR)
    cfg = _cfg(strategy_ref="template:breakout", parameters={"entry": 48, "exit": 24})
    t1 = make_trader(cfg, tmp_path / "p", PRODUCT, clock=lambda: 0)
    t1.warm_up(bars.iloc[:3990])
    assert len(t1.history) == 3990
    assert target_fraction(t1.spec, cfg.policy, t1.history, cfg.granularity) == 1.0
    t1.ledger.close()
    t2 = make_trader(cfg, tmp_path / "p", PRODUCT, clock=lambda: 0)  # restart
    t2.warm_up(bars)
    assert len(t2.history) == 4000
    assert target_fraction(t2.spec, cfg.policy, t2.history, cfg.granularity) == 1.0


def test_an_old_bar_fetched_now_is_stale(tmp_path):
    now = T0 + 10 * HOUR

    class OldCandles:
        def candles(self, *_):
            return SimpleNamespace(body={"candles": [{"start": now - 5 * HOUR, "open": 100, "high": 100,
                                                      "low": 100, "close": 100, "volume": 10}]})

    bar = CoinbaseCandlePoller(OldCandles(), "BTC-USD", "ONE_HOUR", clock=lambda: now).poll()[0]
    trader = make_trader(_cfg(strategy_ref="template:buy_and_hold", parameters={}), tmp_path / "stale", PRODUCT,
                         clock=lambda: now)
    record = trader.on_bar(bar)
    assert not record["decision"]["approved"] and record["intent"] is None
    health = trader.write_health()
    assert health["data_age_seconds"] == 4 * HOUR and health["stale"]


def test_every_bar_is_marked_even_without_a_rebalance(tmp_path):
    bars = _bars(60)
    trader = run_replay(_cfg(policy=PortfolioPolicy(rebalance_every=24)), bars, tmp_path / "m", PRODUCT)
    marks = trader.ledger.marks()
    assert len(marks) == len(bars) and len(trader.ledger.decisions()) < len(bars)
    final_close = Decimal(str(bars["close"].iloc[-1]))
    assert marks.iloc[-1]["equity"] == pytest.approx(float(trader.equity(final_close)))


def test_parity_is_clean_for_a_replay_with_partial_positions_and_a_band(tmp_path):
    from quant_research.paper.fleet import Roster, RosterEntry
    from quant_research.paper.parity import paper_parity

    bars = _bars(400)
    policy = PortfolioPolicy(vol_target=0.5, vol_lookback=24, band=0.05)
    cfg = _cfg(policy=policy, buy_cash_buffer=0.0)
    trader = run_replay(cfg, bars, tmp_path / "p", PRODUCT)
    entry = RosterEntry(name="t", product="BTC-USD", strategy_ref=cfg.strategy_ref, parameters=cfg.parameters,
                        policy=policy, fee_rate=0.001, slippage_bps=2.0)
    out = paper_parity(entry, Roster(), trader.ledger, bars, None)
    assert out["checked"] > 100 and out["skipped"] == 0
    assert out["target_mismatches"] == [] and out["order_mismatches"] == [] and out["size_mismatches"] == []
    assert out["fills"] > 4 and abs(out["fill_vs_next_open_bps"]) < 1e-6 and out["fee_rate_error"] < 1e-9
    assert out["allowed"] == {}
    path = out["path"]
    assert path["bars"] > 100
    assert abs(path["equity_gap_bps"]) < 0.01 and abs(path["execution_bps"]) < 0.01
    assert abs(path["exposure_gap_pp"]) < 0.001


def _parity_run(tmp_path, roster_risk=None, **cfg_kw):
    from quant_research.paper.fleet import Roster, RosterEntry
    from quant_research.paper.parity import paper_parity

    bars = _bars(400)
    cfg = _cfg(**cfg_kw)
    trader = run_replay(cfg, bars, tmp_path / "p", PRODUCT)
    entry = RosterEntry(name="t", product="BTC-USD", strategy_ref=cfg.strategy_ref, parameters=cfg.parameters,
                        policy=cfg.policy, fee_rate=0.001, slippage_bps=2.0)
    roster = Roster(risk=roster_risk) if roster_risk else Roster()
    return trader, entry, roster, bars, paper_parity(entry, roster, trader.ledger, bars, None)


def test_parity_counts_the_order_cap_as_an_allowed_difference(tmp_path):
    risk = RiskLimits(max_order_notional=3000, max_orders_per_hour=100, max_daily_loss_fraction=0.99)
    *_, out = _parity_run(tmp_path, roster_risk=risk, risk=risk, buy_cash_buffer=0.0)
    assert out["order_mismatches"] == [] and out["size_mismatches"] == []
    assert out["allowed"]["order_cap"] > 2
    assert abs(out["path"]["exposure_gap_pp"]) < 100 and out["path"]["sizing_timing_bps"] != 0


def test_parity_counts_the_cash_buffer_as_an_allowed_difference(tmp_path):
    *_, out = _parity_run(tmp_path)  # default 1% buffer: full-exposure buys stop short of the backtest's size
    assert out["size_mismatches"] == [] and out["allowed"].get("cash_buffer", 0) > 2


def test_parity_catches_a_wrong_order_size(tmp_path):
    import json

    from quant_research.paper.parity import paper_parity

    trader, entry, roster, bars, _ = _parity_run(tmp_path, buy_cash_buffer=0.0)
    row = trader.ledger._conn.execute(
        "SELECT id, data FROM decisions WHERE json_extract(data, '$.intent') IS NOT NULL ORDER BY id LIMIT 1"
    ).fetchone()
    data = json.loads(row[1])
    data["intent"]["base_size"] = str(Decimal(data["intent"]["base_size"]) * Decimal("1.0005"))
    trader.ledger._conn.execute("UPDATE decisions SET data=? WHERE id=?", (json.dumps(data), row[0]))
    out = paper_parity(entry, roster, trader.ledger, bars, None)
    assert len(out["size_mismatches"]) == 1 and data["bar_open"] in out["size_mismatches"][0]


def test_paper_buy_size_follows_the_backtest_rule(tmp_path):
    from quant_research.paper.parity import rule_quantity

    # Partial targets, so the target (not the cash) limits the buys and the slippage term matters.
    policy = PortfolioPolicy(vol_target=0.5, vol_lookback=24, band=0.05)
    trader, *_ = _parity_run(tmp_path, buy_cash_buffer=0.0, policy=policy)
    marks = {r["bar_open"]: r for r in trader.ledger.marks().to_dict("records")}
    buys = [d for d in trader.ledger.decisions() if d.get("intent") and d["intent"]["side"] == "buy"]
    assert sum(0 < d["decision"]["target_fraction"] < 0.95 for d in buys) > 2
    for d in buys:
        m = marks[d["bar_open"]]
        rule = rule_quantity("buy", d["decision"]["target_fraction"], m["base"], m["quote"], m["price"], 0.001, 2.0)
        assert float(d["intent"]["base_size"]) == pytest.approx(rule, abs=2e-8)


def test_ledger_waits_for_a_busy_database(tmp_path):
    from quant_research.paper.ledger import Ledger

    ledger = Ledger(tmp_path / "l.db")
    assert ledger._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
    ledger.close()
