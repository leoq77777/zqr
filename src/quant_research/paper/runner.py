"""Paper-trading process entry points: offline replay and live loop on Coinbase public data."""

from __future__ import annotations

import logging
import signal
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from quant_research.data.bars import to_epoch
from quant_research.data.coinbase import CoinbasePublicClient
from quant_research.paper.broker import SimulatedBroker
from quant_research.paper.feeds import SEED_BARS, CoinbaseCandlePoller, ReplayFeed, coinbase_mid_price
from quant_research.paper.ledger import Ledger
from quant_research.paper.models import D, ProductSpec
from quant_research.paper.trader import PaperConfig, PaperTrader

log = logging.getLogger(__name__)


def make_trader(cfg: PaperConfig, workdir: Path, product: ProductSpec, paths=None, clock=time.time,
                faults: list[str] | None = None, broker: Any = None) -> PaperTrader:
    ledger = Ledger(workdir / "paper.db")
    if broker is None:
        balances = _restore_sim_balances(ledger, cfg, product)
        broker = SimulatedBroker(product, balances, cfg.fee_rate, cfg.slippage_bps, faults)
    if cfg.portfolio is not None:
        from quant_research.paper.portfolio_trader import PortfolioTrader

        return PortfolioTrader(cfg, ledger, broker, product, workdir, paths=paths, clock=clock)
    return PaperTrader(cfg, ledger, broker, product, workdir, paths=paths, clock=clock)


def _restore_sim_balances(ledger: Ledger, cfg: PaperConfig, product: ProductSpec) -> dict[str, Decimal]:
    """The local simulator's state is the ledger itself, so a restarted simulator resumes from it."""
    if ledger.get("initial_balances") is None:
        return {product.base_currency: cfg.initial_base, product.quote_currency: cfg.initial_quote}
    return ledger.balances(product.base_currency, product.quote_currency)


def run_replay(cfg: PaperConfig, bars: pd.DataFrame, workdir: Path, product: ProductSpec, warmup_bars: int = 0,
               paths=None, faults: list[str] | None = None, delay_seconds: float = 1.0) -> PaperTrader:
    """Replay a snapshot through the full paper stack. Orders decided at bar t fill at bar t+1's open."""
    state = {"now": 0.0}
    trader = make_trader(cfg, workdir, product, paths=paths, clock=lambda: state["now"], faults=faults)
    if warmup_bars:
        trader.warm_up(bars.iloc[:warmup_bars])
    for bar in ReplayFeed(bars.iloc[warmup_bars:]):
        open_ts = float(to_epoch(pd.Series([bar["bar_open_time"]]))[0])
        state["now"] = open_ts
        trader.process_market(D(bar["open"]), open_ts)
        state["now"] = float(to_epoch(pd.Series([bar["available_at"]]))[0]) + delay_seconds
        trader.on_bar(bar)
    return trader


def run_live(cfg: PaperConfig, workdir: Path, client: CoinbasePublicClient, product: ProductSpec,
             poll_seconds: float = 30.0, max_iterations: int | None = None, paths=None) -> PaperTrader:
    """Live paper loop: completed bars from public REST, fills simulated at the public mid price."""
    trader = make_trader(cfg, workdir, product, paths=paths)
    trader.recover()
    poller = CoinbaseCandlePoller(client, cfg.instrument, cfg.granularity)
    trader.warm_up(poller.history(cfg.history_bars or SEED_BARS))
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    i = 0
    while not stop["flag"] and (max_iterations is None or i < max_iterations):
        i += 1
        try:
            for bar in poller.poll():
                trader.on_bar(bar)
            if trader.live_orders():
                trader.process_market(coinbase_mid_price(client, cfg.instrument), time.time())
        except Exception as exc:  # keep the loop alive; health file shows staleness
            trader._log("ALERT", "loop_error", error=f"{type(exc).__name__}: {exc}")
        trader.write_health()
        time.sleep(poll_seconds)
    trader._log("INFO", "stopped")
    return trader
