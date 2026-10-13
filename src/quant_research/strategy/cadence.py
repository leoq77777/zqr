"""Decision cadence on hourly bars (docs/library/seeds.md section 1.2).

A bar's nominal close is its open plus one hour. Bar i is a boundary when its close falls in a later calendar period
(UTC day, Monday-based week, month) than the close of bar i-1, so the bar closing at 00:00 UTC decides, and after a
gap the first bar past the boundary does. Only bars up to i are used, so sampling never looks ahead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quant_research.data.bars import to_epoch

HOUR = 3600
DAY = 86400
BARS_PER_DAY = 24
CADENCES = ("bar", "daily", "weekly", "monthly")


def close_epochs(bars: pd.DataFrame) -> np.ndarray:
    return to_epoch(bars["bar_open_time"]) + HOUR


def period_keys(epochs: np.ndarray, cadence: str) -> np.ndarray:
    days = np.asarray(epochs, dtype="int64") // DAY
    if cadence == "daily":
        return days
    if cadence == "weekly":
        return (days - 4) // 7  # 1970-01-05, day 4, was a Monday
    if cadence == "monthly":
        return np.asarray(epochs, dtype="int64").astype("datetime64[s]").astype("datetime64[M]").astype("int64")
    raise ValueError(f"unknown cadence {cadence!r}; expected one of {CADENCES}")


def boundaries(bars: pd.DataFrame, cadence: str) -> np.ndarray:
    n = len(bars)
    if cadence == "bar":
        return np.ones(n, dtype=bool)
    keys = period_keys(close_epochs(bars), cadence)
    out = np.zeros(n, dtype=bool)
    out[1:] = keys[1:] != keys[:-1]
    return out


def sample_and_hold(signal: np.ndarray, at: np.ndarray) -> np.ndarray:
    """The signal's value at the latest boundary at or before each bar; 0 before the first boundary, and a NaN
    signal at a boundary (incomplete lookback) holds 0."""
    sampled = np.where(at, np.nan_to_num(np.asarray(signal, dtype=float), nan=0.0), np.nan)
    return pd.Series(sampled).ffill().fillna(0.0).to_numpy()


def daily_closes(bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Indices of the daily boundary bars and their closes: the daily close series the daily templates use."""
    idx = np.flatnonzero(boundaries(bars, "daily"))
    return idx, bars["close"].to_numpy(float)[idx]


def days(bars_count: int) -> int:
    if bars_count % BARS_PER_DAY:
        raise ValueError(f"{bars_count} bars is not a whole number of days")
    return bars_count // BARS_PER_DAY
