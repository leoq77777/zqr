"""Built-in parameterised strategies.

Exposure contract v0.2 (docs/contracts-v2.md section 3), shared with plugins: given bars up to and including
row t, the value at row t is the exposure proposal in [0, 1] decided at bar t's close. The portfolio policy turns
proposals into target weights; the engine executes them at bar t+1's open.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, PositiveFloat, PositiveInt, model_validator

from quant_research.strategy.cadence import BARS_PER_DAY, boundaries, daily_closes, days, sample_and_hold

Cadence = Literal["bar", "daily", "weekly", "monthly"]


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


def _whole_days(*values: int) -> None:
    for v in values:
        if v % BARS_PER_DAY:
            raise ValueError(f"{v} bars is not a whole number of days ({BARS_PER_DAY} bars each)")


class TsmomParams(_Params):
    lookback: PositiveInt
    cadence: Cadence


class MaFilterParams(_Params):
    window: PositiveInt
    cadence: Cadence


class EwmaTrendParams(_Params):
    """Windows in hourly bars, whole days; the signal runs on daily closes (Baz et al. 2015)."""

    fast: PositiveInt
    slow: PositiveInt
    vol_window: PositiveInt
    norm_window: PositiveInt

    @model_validator(mode="after")
    def _check(self):
        if self.fast >= self.slow:
            raise ValueError("fast must be < slow")
        _whole_days(self.fast, self.slow, self.vol_window, self.norm_window)
        return self


class VolTargetParams(_Params):
    window: PositiveInt
    target_vol: PositiveFloat

    @model_validator(mode="after")
    def _check(self):
        _whole_days(self.window)
        if days(self.window) < 2:
            raise ValueError("window needs at least 2 days")
        return self


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


def _signal(condition: pd.Series, valid: pd.Series) -> np.ndarray:
    return np.where(valid.to_numpy(), condition.to_numpy(float), np.nan)


def _tsmom(bars: pd.DataFrame, p: TsmomParams) -> pd.Series:
    close = bars["close"].reset_index(drop=True)
    ret = close / close.shift(p.lookback) - 1.0
    held = sample_and_hold(_signal(ret > 0, ret.notna()), boundaries(bars, p.cadence))
    return pd.Series(held, index=bars.index)


def _ma_filter(bars: pd.DataFrame, p: MaFilterParams) -> pd.Series:
    close = bars["close"].reset_index(drop=True)
    sma = close.rolling(p.window, min_periods=p.window).mean()
    held = sample_and_hold(_signal(close > sma, sma.notna()), boundaries(bars, p.cadence))
    return pd.Series(held, index=bars.index)


def _on_daily(bars: pd.DataFrame, daily_fn: Callable[[pd.Series], pd.Series]) -> pd.Series:
    idx, closes = daily_closes(bars)
    signal = np.full(len(bars), np.nan)
    signal[idx] = daily_fn(pd.Series(closes)).replace([np.inf, -np.inf], np.nan).to_numpy(float)
    held = sample_and_hold(signal, boundaries(bars, "daily"))
    return pd.Series(held, index=bars.index)


def baz_response(z):
    return z * np.exp(-(z ** 2) / 4.0) / 0.89


def _ewma_trend(bars: pd.DataFrame, p: EwmaTrendParams) -> pd.Series:
    def daily(price: pd.Series) -> pd.Series:
        # EWMA with time scale n: decay (n - 1) / n, i.e. alpha 1 / n (Baz et al. 2015, eq. 28).
        x = (price.ewm(alpha=1 / days(p.fast), adjust=False).mean()
             - price.ewm(alpha=1 / days(p.slow), adjust=False).mean())
        y = x / price.rolling(days(p.vol_window), min_periods=days(p.vol_window)).std()
        z = y / y.rolling(days(p.norm_window), min_periods=days(p.norm_window)).std()
        return baz_response(z).clip(lower=0.0)

    return _on_daily(bars, daily)


def _vol_target(bars: pd.DataFrame, p: VolTargetParams) -> pd.Series:
    def daily(price: pd.Series) -> pd.Series:
        n = days(p.window)
        vol = price.pct_change().rolling(n, min_periods=n).std() * np.sqrt(365.0)
        scale = (p.target_vol / vol).clip(upper=1.0)
        return scale.where(vol != 0.0, 1.0).where(vol.notna())

    return _on_daily(bars, daily)


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
        Template("tsmom", "1", TsmomParams, _tsmom,
                 "time-series momentum: long while the lookback return > 0, decided at the cadence (UTC)"),
        Template("ma_filter", "1", MaFilterParams, _ma_filter,
                 "trend filter: long while close > SMA(window), decided at the cadence (UTC)"),
        Template("ewma_trend", "1", EwmaTrendParams, _ewma_trend,
                 "Baz et al. EWMA crossover with response function on daily closes, exposure max(0, u)"),
        Template("vol_target", "1", VolTargetParams, _vol_target,
                 "min(1, target_vol / realised vol of daily returns over window); the portfolio overlay"),
    )
}


def get_template(name: str) -> Template:
    if name not in TEMPLATES:
        raise KeyError(f"unknown template {name!r}; available: {sorted(TEMPLATES)}")
    return TEMPLATES[name]
