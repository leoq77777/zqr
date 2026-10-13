"""Built-in parameterised strategies.

Exposure contract v0.2 (docs/contracts-v2.md section 3), shared with plugins: given bars up to and including
row t, the value at row t is the exposure proposal in [0, 1] decided at bar t's close. The portfolio policy turns
proposals into target weights; the engine executes them at bar t+1's open.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, PositiveInt, model_validator


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoParams(_Params):
    pass


class SmaCrossParams(_Params):
    fast: PositiveInt
    slow: PositiveInt

    @model_validator(mode="after")
    def _order(self):
        if self.fast >= self.slow:
            raise ValueError("fast must be < slow")
        return self


class MomentumParams(_Params):
    lookback: PositiveInt
    threshold: float = 0.0


class BreakoutParams(_Params):
    entry: PositiveInt
    exit: PositiveInt


def _buy_and_hold(bars: pd.DataFrame, p: NoParams) -> pd.Series:
    return pd.Series(1.0, index=bars.index)


def _cash(bars: pd.DataFrame, p: NoParams) -> pd.Series:
    return pd.Series(0.0, index=bars.index)


def _sma_cross(bars: pd.DataFrame, p: SmaCrossParams) -> pd.Series:
    close = bars["close"]
    fast = close.rolling(p.fast, min_periods=p.fast).mean()
    slow = close.rolling(p.slow, min_periods=p.slow).mean()
    return (fast > slow).astype(float)


def _momentum(bars: pd.DataFrame, p: MomentumParams) -> pd.Series:
    ret = bars["close"] / bars["close"].shift(p.lookback) - 1.0
    return (ret > p.threshold).astype(float)


def _breakout(bars: pd.DataFrame, p: BreakoutParams) -> pd.Series:
    """Donchian channel: enter above the prior `entry`-bar high, exit below the prior `exit`-bar low."""
    upper = bars["high"].rolling(p.entry, min_periods=p.entry).max().shift(1).to_numpy()
    lower = bars["low"].rolling(p.exit, min_periods=p.exit).min().shift(1).to_numpy()
    close = bars["close"].to_numpy()
    out = np.zeros(len(close))
    pos = 0.0
    for i in range(len(close)):
        if pos == 0.0 and not np.isnan(upper[i]) and close[i] > upper[i]:
            pos = 1.0
        elif pos == 1.0 and not np.isnan(lower[i]) and close[i] < lower[i]:
            pos = 0.0
        out[i] = pos
    return pd.Series(out, index=bars.index)


@dataclass(frozen=True)
class Template:
    name: str
    version: str
    params_model: type[_Params]
    fn: Callable[[pd.DataFrame, Any], pd.Series]
    description: str

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.params_model.model_validate(params).model_dump()

    def __call__(self, bars: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
        return self.fn(bars, self.params_model.model_validate(params))


TEMPLATES: dict[str, Template] = {
    t.name: t
    for t in (
        Template("buy_and_hold", "1", NoParams, _buy_and_hold, "always fully long"),
        Template("cash", "1", NoParams, _cash, "always flat"),
        Template("sma_cross", "1", SmaCrossParams, _sma_cross, "long while SMA(fast) > SMA(slow)"),
        Template("momentum", "1", MomentumParams, _momentum, "long while lookback return > threshold"),
        Template("breakout", "1", BreakoutParams, _breakout, "Donchian channel breakout"),
    )
}


def get_template(name: str) -> Template:
    if name not in TEMPLATES:
        raise KeyError(f"unknown template {name!r}; available: {sorted(TEMPLATES)}")
    return TEMPLATES[name]
