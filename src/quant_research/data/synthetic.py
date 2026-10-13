"""Synthetic control datasets with known ground truth, for testing the research pipeline itself.

- random_walk: no predictable structure (negative control). Any accepted strategy is a false positive.
- regime_trend: persistent up/down drift regimes with geometric durations (positive control). Trend following
  has a genuine after-cost edge, so a working pipeline should be able to find and accept it.

The generator settings are the answer key; they are written outside the snapshot manifest.
"""

from __future__ import annotations

import json
import math
import time
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel

from quant_research.data.snapshot import freeze_dataset
from quant_research.data.validate import validate_bars
from quant_research.paths import Paths
from quant_research.timeutil import granularity_seconds, parse_granularity

Kind = Literal["random_walk", "regime_trend"]
VENUE = "lab"


class SyntheticSpec(BaseModel):
    kind: Kind
    instrument: str
    granularity: str = "ONE_DAY"
    start: int = 1609459200  # 2021-01-01
    bars: int = 2110
    seed: int = 0
    vol: float = 0.03  # per-bar standard deviation of log returns
    tail_df: float = 5.0  # Student-t innovations (fat tails), scaled to unit variance
    drift: float = 0.0  # per-bar log drift; regime_trend uses +drift / -drift
    mean_regime_bars: float = 30.0


def generate(spec: SyntheticSpec) -> tuple[pd.DataFrame, np.ndarray]:
    """Raw candles in the Coinbase-normalised layout, plus the per-bar regime (+1/-1, or 0 for no regime)."""
    rng = np.random.default_rng(spec.seed)
    n = spec.bars
    z = rng.standard_t(spec.tail_df, n) * math.sqrt((spec.tail_df - 2) / spec.tail_df)
    if spec.kind == "regime_trend":
        regime = np.empty(n)
        state, i = 1.0 if rng.random() < 0.5 else -1.0, 0
        while i < n:
            length = int(rng.geometric(1 / spec.mean_regime_bars))
            regime[i:i + length] = state
            state, i = -state, i + length
        mu = spec.drift * regime
    else:
        regime = np.zeros(n)
        mu = np.full(n, spec.drift)
    log_ret = mu + spec.vol * z
    close = 100 * np.exp(np.cumsum(log_ret))
    open_ = np.concatenate([[100.0], close[:-1]])
    wick = spec.vol * np.abs(rng.standard_normal((2, n))) * 0.5
    high = np.maximum(open_, close) * np.exp(wick[0])
    low = np.minimum(open_, close) * np.exp(-wick[1])
    step = granularity_seconds(parse_granularity(spec.granularity))
    starts = spec.start + step * np.arange(n)
    raw = pd.DataFrame({"start": starts, "open": open_, "high": high, "low": low, "close": close,
                        "volume": rng.lognormal(10, 0.5, n), "source_version": "synthetic-v1",
                        "fetched_at": float(starts[-1] + 2 * step)})
    return raw, regime


def freeze_synthetic(paths: Paths, spec: SyntheticSpec) -> dict:
    raw, regime = generate(spec)
    step = granularity_seconds(parse_granularity(spec.granularity))
    bars, report = validate_bars(raw, VENUE, spec.instrument, spec.granularity, spec.start,
                                 spec.start + step * spec.bars)
    manifest = freeze_dataset(paths, bars, report, extra={"synthetic": True})
    key_dir = paths.root / "controls"
    key_dir.mkdir(parents=True, exist_ok=True)
    flips = int((np.diff(regime) != 0).sum())
    (key_dir / f"{manifest['dataset_id']}.json").write_text(json.dumps(
        {"spec": spec.model_dump(), "regime_flips": flips, "created_at": time.time()}, indent=2))
    return manifest
