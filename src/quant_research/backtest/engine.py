"""Deterministic long/flat spot backtest with an explicit cash/position ledger.

Execution contract (docs/contracts-v2.md section 4):
- target[t] is decided with information up to bar t's close and filled at bar t+1+delay's open;
  a target whose fill bar lies beyond the window is never filled.
- market orders only: buys fill at open*(1+slippage), sells at open*(1-slippage); taker fee on notional.
  With `adverse_fill`, the reference price is the worse of the decision close and the fill open.
- buys are capped so cash never goes negative; no leverage, no shorting.
- with `base_increment`, traded quantities are rounded down to the increment.
- a NaN target means no order at that bar; the position is left to drift.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, NonNegativeFloat, NonNegativeInt, PositiveFloat

from quant_research import ENGINE_VERSION
from quant_research.backtest.rebalance import EPS as _EPS
from quant_research.backtest.rebalance import needs_trade
from quant_research.data.bars import to_epoch
from quant_research.hashing import hash_obj


class CostConfig(BaseModel):
    fee_rate: NonNegativeFloat = 0.006  # taker fee fraction; replace with the account's real tier
    slippage_bps: NonNegativeFloat = 5.0
    source: str = "assumed: Coinbase lowest public taker tier, unverified for this account"

    def scaled(self, factor: float) -> CostConfig:
        return CostConfig(fee_rate=self.fee_rate * factor, slippage_bps=self.slippage_bps * factor,
                          source=f"{self.source} x{factor:g}")


class EngineConfig(BaseModel):
    initial_cash: PositiveFloat = 10_000.0
    costs: CostConfig = Field(default_factory=CostConfig)
    min_trade_notional: NonNegativeFloat = 1.0
    rebalance_tolerance: NonNegativeFloat = 0.0  # skip trades smaller than this fraction of equity
    execution_delay_bars: NonNegativeInt = 0
    adverse_fill: bool = False
    base_increment: PositiveFloat | None = None  # product quantity step; None = continuous

    def sensitivity(self, case: str) -> EngineConfig:
        """Fixed sensitivity cases of contracts-v2 section 4."""
        cases = {"costs_x2": {"costs": self.costs.scaled(2.0)},
                 "delay_1": {"execution_delay_bars": 1},
                 "delay_1_adverse": {"execution_delay_bars": 1, "adverse_fill": True}}
        return self.model_copy(update=cases[case])

    def config_id(self) -> str:
        return "engine-" + hash_obj({"engine_version": ENGINE_VERSION, **self.model_dump()})[:12]


@dataclass
class BacktestResult:
    equity: pd.DataFrame  # per scored bar, valued at close
    fills: pd.DataFrame
    trips: pd.DataFrame
    gap_crossings: int = 0
    meta: dict = field(default_factory=dict)


FILL_COLUMNS = ["time", "bar_index", "side", "qty", "price", "mark_price", "notional", "fee", "slippage_cost",
                "target"]
TRIP_COLUMNS = ["entry_time", "exit_time", "bars_held", "pnl", "return"]


def run_backtest(
    bars: pd.DataFrame,
    targets: np.ndarray,
    config: EngineConfig,
    window: tuple[int, int] | None = None,
    step_seconds: int | None = None,
) -> BacktestResult:
    bars = bars.reset_index(drop=True)
    if len(targets) != len(bars):
        raise ValueError("targets and bars must align")
    opens_ts = to_epoch(bars["bar_open_time"])
    in_window = np.ones(len(bars), bool) if window is None else (opens_ts >= window[0]) & (opens_ts < window[1])
    idx = np.flatnonzero(in_window)
    if not len(idx):
        raise ValueError("no bars in the scored window")
    if not np.all(np.diff(idx) == 1):
        raise ValueError("scored window must be contiguous in the bar index")

    o = bars["open"].to_numpy(float)
    c = bars["close"].to_numpy(float)
    times = bars["bar_open_time"]
    fee_rate = config.costs.fee_rate
    slip = config.costs.slippage_bps / 10_000.0
    delay = config.execution_delay_bars
    inc = config.base_increment

    def _round(q: float) -> float:
        return q if inc is None else math.floor(q / inc + 1e-9) * inc

    cash, qty = float(config.initial_cash), 0.0
    fills, trips = [], []
    trip_entry: tuple | None = None  # (time, bar_index, cash_before_entry_as_value)
    eq_rows = []
    gap_crossings = 0

    for i in idx:
        decided = i - 1 - delay
        if i > 0 and step_seconds and opens_ts[i] - opens_ts[i - 1] != step_seconds:
            gap_crossings += 1
        target = float(targets[decided]) if decided >= 0 else math.nan
        if not math.isnan(target):  # NaN = no instruction at this bar (warmup or unscheduled rebalance)
            equity_open = cash + qty * o[i]
            delta = target * equity_open - qty * o[i]
            full_exit = target == 0.0 and qty > 0
            if needs_trade(target, qty * o[i], equity_open, tolerance=config.rebalance_tolerance,
                           min_notional=config.min_trade_notional):
                value_before = equity_open
                if delta > 0:
                    ref = max(o[i], c[decided]) if config.adverse_fill else o[i]
                    px = ref * (1 + slip)
                    q = _round(min(delta, cash / (1 + fee_rate)) / px)
                    notional = q * px
                    if q > 0 and notional >= max(config.min_trade_notional, _EPS * equity_open):
                        fee = notional * fee_rate
                        cash = max(cash - notional - fee, 0.0)
                        was_flat = qty == 0
                        qty += q
                        fills.append((times[i], i, "buy", q, px, o[i], notional, fee, q * (px - o[i]), target))
                        if was_flat:
                            trip_entry = (times[i], i, value_before)
                elif delta < 0 or full_exit:
                    ref = min(o[i], c[decided]) if config.adverse_fill else o[i]
                    px = ref * (1 - slip)
                    q = qty if target == 0.0 else _round(min(qty, -delta / o[i]))
                    if q > 0:
                        notional = q * px
                        fee = notional * fee_rate
                        cash += notional - fee
                        qty -= q
                        if qty <= 1e-15:
                            qty = 0.0
                        fills.append((times[i], i, "sell", q, px, o[i], notional, fee, q * (o[i] - px), target))
                        if qty == 0.0 and trip_entry is not None:
                            pnl = cash - trip_entry[2]
                            trips.append((trip_entry[0], times[i], i - trip_entry[1], pnl, pnl / trip_entry[2]))
                            trip_entry = None
        equity = cash + qty * c[i]
        eq_rows.append((times[i], cash, qty, c[i], equity, qty * c[i] / equity if equity > 0 else 0.0))

    equity_df = pd.DataFrame(eq_rows, columns=["time", "cash", "qty", "close", "equity", "exposure"])
    last_close = c[idx[-1]]
    return BacktestResult(
        equity=equity_df,
        fills=pd.DataFrame(fills, columns=FILL_COLUMNS),
        trips=pd.DataFrame(trips, columns=TRIP_COLUMNS),
        gap_crossings=gap_crossings,
        meta={"engine_version": ENGINE_VERSION, "open_trip": trip_entry is not None,
              "liquidation_equity": cash + qty * last_close * (1 - slip) * (1 - fee_rate)},
    )
