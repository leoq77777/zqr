"""Turn raw candle responses into clean bars and a quality report. Never fills gaps silently."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from quant_research.data.bars import BAR_COLUMNS, to_epoch, to_utc
from quant_research.timeutil import ceil_to, granularity_seconds, parse_granularity

FillPolicy = Literal["none", "ffill_flat"]


class Gap(BaseModel):
    start: int  # first missing bar open (epoch s)
    end: int  # exclusive
    bars: int


class ValidationReport(BaseModel):
    venue: str
    instrument: str
    granularity: str
    requested_start: int
    requested_end: int
    raw_rows: int = 0
    out_of_range: int = 0
    duplicates: int = 0
    duplicate_conflicts: int = 0
    incomplete_dropped: int = 0
    misaligned_dropped: int = 0
    invalid_dropped: dict[str, int] = Field(default_factory=dict)
    rows: int = 0
    expected_rows: int = 0
    coverage: float = 0.0
    actual_first: int | None = None
    actual_last: int | None = None
    leading_missing_bars: int = 0
    trailing_missing_bars: int = 0
    gaps: list[Gap] = Field(default_factory=list)
    internal_gap_bars: int = 0
    fill_policy: FillPolicy = "none"
    filled_rows: int = 0

    @property
    def has_unfilled_gaps(self) -> bool:
        return self.internal_gap_bars > 0 and self.fill_policy == "none"


def load_raw_candles(raw_dir: Path) -> pd.DataFrame:
    """Read every persisted window in `raw_dir` into one frame (may contain overlaps/duplicates)."""
    rows = []
    for path in sorted(raw_dir.glob("*_*.json")):
        payload = json.loads(path.read_text())
        if payload.get("status") != 200:
            continue
        for c in payload["body"].get("candles", []):
            rows.append(
                {
                    "venue": payload["venue"],
                    "instrument": payload["product_id"],
                    "granularity": payload["granularity"],
                    "start": int(c["start"]),
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "volume": float(c["volume"]),
                    "source_version": payload["source_version"],
                    "fetched_at": float(payload["fetched_at"]),
                }
            )
    return pd.DataFrame(rows)


def validate_bars(
    raw: pd.DataFrame,
    venue: str,
    instrument: str,
    granularity: str,
    start: int,
    end: int,
    fill_policy: FillPolicy = "none",
) -> tuple[pd.DataFrame, ValidationReport]:
    """Clip to [start, end), dedupe, drop incomplete/invalid bars and report gaps.

    `raw` needs columns start (epoch s), open, high, low, close, volume, fetched_at, source_version.
    """
    granularity = parse_granularity(granularity)
    step = granularity_seconds(granularity)
    rep = ValidationReport(venue=venue, instrument=instrument, granularity=granularity,
                           requested_start=start, requested_end=end, fill_policy=fill_policy,
                           raw_rows=len(raw))
    df = raw.copy()
    if df.empty:
        df = pd.DataFrame(columns=["start", "open", "high", "low", "close", "volume",
                                   "fetched_at", "source_version"])

    in_range = (df["start"] >= start) & (df["start"] < end)
    rep.out_of_range = int((~in_range).sum())
    df = df[in_range]

    aligned = df["start"] % step == 0
    rep.misaligned_dropped = int((~aligned).sum())
    df = df[aligned]

    # Bars whose close was after the fetch time were still forming when downloaded.
    complete = df["start"] + step <= df["fetched_at"]
    rep.incomplete_dropped = int((~complete).sum())
    df = df[complete]

    df = df.sort_values(["start", "fetched_at"], kind="mergesort")
    dup_mask = df.duplicated("start", keep="last")
    rep.duplicates = int(dup_mask.sum())
    if rep.duplicates:
        values = df[["start", "open", "high", "low", "close", "volume"]]
        conflicts = values.groupby("start").nunique().gt(1).any(axis=1)
        rep.duplicate_conflicts = int(conflicts.sum())
    df = df[~dup_mask]

    invalid: dict[str, pd.Series] = {
        "non_positive_price": (df[["open", "high", "low", "close"]] <= 0).any(axis=1),
        "negative_volume": df["volume"] < 0,
        "non_finite": ~np.isfinite(df[["open", "high", "low", "close", "volume"]]).all(axis=1),
        "ohlc_inconsistent": (df["high"] < df[["open", "close", "low"]].max(axis=1))
        | (df["low"] > df[["open", "close", "high"]].min(axis=1)),
    }
    bad = pd.Series(False, index=df.index)
    for reason, mask in invalid.items():
        mask = mask & ~bad
        if mask.any():
            rep.invalid_dropped[reason] = int(mask.sum())
        bad |= mask
    df = df[~bad]

    grid_start = ceil_to(start, step)
    last_fetch = float(raw["fetched_at"].max()) if len(raw) else start
    # Only bars that had closed by the latest fetch can be expected.
    grid_end = min(end, int(last_fetch) // step * step)
    expected = np.arange(grid_start, max(grid_start, grid_end), step, dtype="int64")
    rep.expected_rows = len(expected)

    present = df["start"].to_numpy(dtype="int64")
    if len(present):
        rep.actual_first, rep.actual_last = int(present.min()), int(present.max())
        missing = np.setdiff1d(expected, present)
        rep.leading_missing_bars = int((missing < rep.actual_first).sum())
        rep.trailing_missing_bars = int((missing > rep.actual_last).sum())
        internal = missing[(missing > rep.actual_first) & (missing < rep.actual_last)]
        rep.gaps = _group_gaps(internal, step)
        rep.internal_gap_bars = len(internal)
    else:
        rep.leading_missing_bars = len(expected)

    bars = pd.DataFrame(
        {
            "venue": venue,
            "instrument": instrument,
            "granularity": granularity,
            "bar_open_time": to_utc(df["start"].to_numpy()),
            "bar_close_time": to_utc((df["start"] + step).to_numpy()),
            "available_at": to_utc((df["start"] + step).to_numpy()),
            "open": df["open"].to_numpy(dtype=float),
            "high": df["high"].to_numpy(dtype=float),
            "low": df["low"].to_numpy(dtype=float),
            "close": df["close"].to_numpy(dtype=float),
            "volume": df["volume"].to_numpy(dtype=float),
            "source_version": df["source_version"].astype(str).to_numpy(),
            "fetched_at": to_utc(df["fetched_at"].to_numpy(dtype=float)),
            "filled": False,
        }
    )
    if fill_policy == "ffill_flat" and rep.gaps:
        bars = _fill_flat(bars, step)
        rep.filled_rows = int(bars["filled"].sum())
    bars = bars[BAR_COLUMNS].reset_index(drop=True)
    rep.rows = len(bars)
    rep.coverage = round(len(present) / rep.expected_rows, 6) if rep.expected_rows else 0.0
    return bars, rep


def _group_gaps(missing: np.ndarray, step: int) -> list[Gap]:
    gaps: list[Gap] = []
    if not len(missing):
        return gaps
    run_start = prev = int(missing[0])
    for t in missing[1:]:
        t = int(t)
        if t != prev + step:
            gaps.append(Gap(start=run_start, end=prev + step, bars=(prev + step - run_start) // step))
            run_start = t
        prev = t
    gaps.append(Gap(start=run_start, end=prev + step, bars=(prev + step - run_start) // step))
    return gaps


def _fill_flat(bars: pd.DataFrame, step: int) -> pd.DataFrame:
    """Explicit policy: a no-trade interval becomes a flat bar at the previous close, volume 0."""
    opens = to_epoch(bars["bar_open_time"])
    full = np.arange(opens[0], opens[-1] + step, step, dtype="int64")
    idx = pd.Index(opens)
    out = bars.set_index(idx).reindex(full)
    filled = out["close"].isna()
    out["close"] = out["close"].ffill()
    for col in ("open", "high", "low"):
        out[col] = out[col].fillna(out["close"])
    out["volume"] = out["volume"].fillna(0.0)
    for col in ("venue", "instrument", "granularity", "source_version"):
        out[col] = out[col].ffill()
    out["fetched_at"] = out["fetched_at"].ffill()
    out["bar_open_time"] = to_utc(full)
    out["bar_close_time"] = to_utc(full + step)
    out["available_at"] = to_utc(full + step)
    out["filled"] = filled.to_numpy()
    return out.reset_index(drop=True)
