"""Canonical bar schema shared by data, backtest and paper modules."""

from __future__ import annotations

import numpy as np
import pandas as pd

BAR_COLUMNS = [
    "venue",
    "instrument",
    "granularity",
    "bar_open_time",
    "bar_close_time",
    "available_at",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source_version",
    "fetched_at",
    "filled",
]
PRICE_COLUMNS = ["open", "high", "low", "close", "volume"]
TIME_COLUMNS = ["bar_open_time", "bar_close_time", "available_at", "fetched_at"]

_EPOCH = pd.Timestamp(0, tz="UTC")


def to_utc(seconds) -> pd.Series | pd.Timestamp:
    return pd.to_datetime(seconds, unit="s", utc=True)


def to_epoch(values: pd.Series) -> np.ndarray:
    """UTC datetimes -> int64 epoch seconds, independent of pandas datetime resolution."""
    return ((pd.Series(values) - _EPOCH) // pd.Timedelta(seconds=1)).to_numpy(dtype="int64")


def empty_bars() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in BAR_COLUMNS})
