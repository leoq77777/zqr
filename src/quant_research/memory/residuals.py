"""Market component removal (appendix A2).

Long/cash strategies all hold the coin part of the time, so their raw returns correlate through it. Each trial's
daily returns are regressed on the coin's close-to-close daily returns; the residuals, winsorised once at
±3 standard deviations, are what the clustering compares. Flat series (never trade) and passive series (return
explained by the coin alone) are flagged and grouped separately. Passive is judged on the winsorised residual
(amendment A13.1), so a single entry-day deviation does not hide a buy-and-hold series.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quant_research.paths import Paths
from quant_research.validation.splits import load_research_segment, slice_window

FLAT_ABS = 1e-12
PASSIVE_SD = 1e-4
WINSOR_SD = 3.0
PASSIVE_RULE = "A13.1"


def market_returns(bars: pd.DataFrame, window: tuple[int, int]) -> pd.Series:
    """Close of each UTC day's last bar over the previous day's; the first day over the first bar's open."""
    b = bars[slice_window(bars, window)]
    day = pd.to_datetime(b["bar_open_time"], utc=True).dt.normalize().dt.tz_localize(None)
    close = b["close"].astype(float).groupby(day.to_numpy()).last()
    prev = np.concatenate([[float(b["open"].iloc[0])], close.to_numpy()[:-1]])
    return pd.Series(close.to_numpy() / prev - 1, index=pd.DatetimeIndex(close.index, name="day"))


def split_market_returns(paths: Paths, split_id: str) -> pd.Series:
    seg = load_research_segment(paths, split_id, "dev")
    return market_returns(seg.bars, seg.window)


def residuals(returns: pd.DataFrame, market: pd.Series, winsorise: bool = True) -> tuple[pd.DataFrame,
                                                                                            pd.DataFrame]:
    """(residuals, flags): flags has columns `beta`, `residual_sd` (of the winsorised residual), `flat`, `passive`."""
    if not returns.index.equals(market.index):
        raise ValueError(f"returns cover {len(returns)} days, the market {len(market)}: different day grids")
    r = returns.to_numpy(float)
    x = np.column_stack([np.ones(len(market)), market.to_numpy(float)])
    coef, *_ = np.linalg.lstsq(x, r, rcond=None)
    eps = r - x @ coef
    sd, mu = eps.std(axis=0, ddof=1), eps.mean(axis=0)
    eps_w = np.clip(eps, mu - WINSOR_SD * sd, mu + WINSOR_SD * sd)
    sd_w = eps_w.std(axis=0, ddof=1)
    flat = np.abs(r).max(axis=0) < FLAT_ABS
    passive = ~flat & (sd_w < PASSIVE_SD)
    flags = pd.DataFrame({"beta": coef[1], "residual_sd": sd_w, "flat": flat, "passive": passive},
                         index=returns.columns)
    return pd.DataFrame(eps_w if winsorise else eps, index=returns.index, columns=returns.columns), flags
