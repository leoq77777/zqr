"""Run a portfolio version through the backtest engine with its decision step (blend, deadband, ladder)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from quant_research.backtest.engine import BacktestResult, run_backtest
from quant_research.portfolio.blend import Blend, blend
from quant_research.portfolio.decide import Decider, DeciderState
from quant_research.portfolio.version import PortfolioVersion
from quant_research.timeutil import granularity_seconds

LOG_COLUMNS = ["bar_open", "blend", "pre_overlay", "overlay", "equity", "position", "pre_state", "level", "yellow",
               "drawdown", "order_target", "executed"]


@dataclass
class PortfolioRun:
    result: BacktestResult
    blend: Blend
    log: pd.DataFrame
    state: DeciderState = field(default_factory=DeciderState)


class _Adapter:
    """The engine's decider protocol over a precomputed blend; keeps one log row per decision."""

    def __init__(self, decider: Decider, bl: Blend, times: pd.Series):
        self.decider, self.blend, self.times = decider, bl, times
        self.rows: list[dict[str, Any]] = []

    def decide(self, i: int, equity: float, position: float) -> float:
        pre = self.decider.state.to_dict()
        t = self.decider.decide(float(self.blend.target[i]), equity, position)
        s = self.decider.state
        self.rows.append({"bar_open": self.times[i], "blend": float(self.blend.target[i]),
                          "pre_overlay": float(self.blend.pre_overlay[i]), "overlay": float(self.blend.overlay[i]),
                          "equity": equity, "position": position, "pre_state": pre, "level": s.level,
                          "yellow": s.yellow, "drawdown": s.drawdown, "order_target": t, "executed": False})
        return t

    def executed(self, i: int, target: float) -> None:
        self.decider.executed(target)
        self.rows[-1]["executed"] = True


def make_decider(version: PortfolioVersion, state: DeciderState | None = None, ladder=None) -> Decider:
    return Decider(version.band, ladder or version.ladder, version.engine_config.min_trade_notional, state)


def run_portfolio(version: PortfolioVersion, bars: pd.DataFrame, window: tuple[int, int] | None = None,
                  paths=None, initial: tuple[float, float] | None = None, state: DeciderState | None = None,
                  bl: Blend | None = None, ladder=None, sandbox=None) -> PortfolioRun:
    """`initial` and `state` start the run from an account's cash, quantity and decider state (paper parity);
    `ladder` replaces the version's ladder (reports show the drawdown without it); `sandbox` runs plugin members
    (backtests only)."""
    bars = bars.reset_index(drop=True)
    bl = bl or blend(version, bars, paths, sandbox)
    decider = make_decider(version, DeciderState.from_dict(state.to_dict()) if state else None, ladder)
    adapter = _Adapter(decider, bl, bars["bar_open_time"])
    res = run_backtest(bars, bl.target, version.engine_config, window=window,
                       step_seconds=granularity_seconds(version.granularity), initial=initial, decider=adapter)
    return PortfolioRun(res, bl, pd.DataFrame(adapter.rows, columns=LOG_COLUMNS), decider.state)
