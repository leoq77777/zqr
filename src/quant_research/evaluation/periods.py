"""Statistics of a recorded run over sub-periods of its window (E1: the early years against 2021-2024).

Computed from the run's equity file, never by re-running. Periods are cut at bar open times; a period's start
equity is the equity at the close of the bar before it (the initial cash for the first period). Buy and hold is
close to close without costs, starting from the close before the period (the first close for the first period).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from quant_research.data.bars import to_epoch
from quant_research.evaluation.split_stats import sharpe_daily
from quant_research.memory.returns import daily_returns
from quant_research.timeutil import to_iso


def _max_drawdown(values: np.ndarray) -> float:
    return float(np.min(values / np.maximum.accumulate(values) - 1.0)) if len(values) else 0.0


def _stats(times: pd.Series, values: np.ndarray, start: float) -> dict[str, float]:
    r = daily_returns(pd.DataFrame({"time": times, "equity": values}), start).to_numpy()
    return {"sharpe_daily": round(sharpe_daily(r), 4), "total_return": round(float(values[-1] / start - 1), 4),
            "max_drawdown": round(_max_drawdown(np.concatenate([[start], values])), 4)}


def period_stats(equity: pd.DataFrame, initial_cash: float, cuts: list[int],
                 log: pd.DataFrame | None = None) -> list[dict[str, Any]]:
    """One row per period between the cuts inside the window, then one for the whole window."""
    t = to_epoch(equity["time"])
    if not len(t):
        return []
    inner = sorted(c for c in set(cuts) if t[0] < c <= t[-1])
    edges = [int(t[0]), *inner, int(t[-1]) + 1]
    spans = [*zip(edges, edges[1:], strict=False)] + ([(edges[0], edges[-1])] if inner else [])
    eq = equity["equity"].to_numpy(float)
    close = equity["close"].to_numpy(float)
    log_t = to_epoch(log["bar_open"]) if log is not None and len(log) else None
    out = []
    for a, b in spans:
        idx = np.flatnonzero((t >= a) & (t < b))
        first = idx[0]
        start = initial_cash if first == 0 else eq[first - 1]
        base = close[first] if first == 0 else close[first - 1]
        times = pd.to_datetime(equity["time"].iloc[idx], utc=True)
        row = {"period": "whole" if (a, b) == (edges[0], edges[-1]) and inner else "part",
               "from": to_iso(int(t[idx[0]])), "to": to_iso(int(t[idx[-1]])),
               "days": int(times.dt.normalize().nunique()), **_stats(times, eq[idx], start),
               "exposure": round(float(equity["exposure"].iloc[idx].mean()), 4),
               "buy_and_hold": _stats(times, close[idx], base)}
        if log_t is not None:
            part = log[(log_t >= a) & (log_t < b)]
            levels = part["level"].value_counts(normalize=True).sort_index()
            row |= {"ladder_time": {str(k): round(float(v), 4) for k, v in levels.items()},
                    "yellow_time": round(float(part["yellow"].mean()), 4) if len(part) else 0.0}
        out.append(row)
    return out
