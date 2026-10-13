"""Completed bars for one product and granularity, anchored at the first stored bar and only ever appended.

Paper strategies compute from the whole store, so stateful rules (a breakout's open position) see the same history
on every restart, and the report can recompute any decision from it.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant_research.data.bars import TIME_COLUMNS, to_epoch
from quant_research.data.snapshot import list_datasets, load_dataset
from quant_research.paths import Paths
from quant_research.timeutil import granularity_seconds


def _utc(bars: pd.DataFrame) -> pd.DataFrame:
    """Parquet reads back ZoneInfo('UTC'), the venue gives timezone.utc; pandas concatenates the two as object."""
    bars = bars.copy()
    for col in TIME_COLUMNS:
        if col in bars:
            bars[col] = pd.to_datetime(bars[col], utc=True)
    return bars


class BarStore:
    def __init__(self, path: Path, granularity: str):
        self.path = Path(path)
        self.step = granularity_seconds(granularity)
        self.bars = _utc(pd.read_parquet(self.path)) if self.path.exists() else pd.DataFrame()

    @classmethod
    def for_feed(cls, paths: Paths, product: str, granularity: str) -> BarStore:
        return cls(paths.paper / "bars" / f"{product}-{granularity}.parquet", granularity)

    def last_open(self) -> int | None:
        return int(to_epoch(self.bars["bar_open_time"])[-1]) if len(self.bars) else None

    def append(self, bars: pd.DataFrame | None) -> pd.DataFrame:
        """Add bars newer than the last stored one; returns the rows added, in time order."""
        if bars is None or not len(bars):
            return pd.DataFrame()
        new = _utc(bars).drop_duplicates("bar_open_time").sort_values("bar_open_time")
        last = self.last_open()
        if last is not None:
            new = new[to_epoch(new["bar_open_time"]) > last]
        new = new.reset_index(drop=True)
        if len(new):
            self.bars = new if not len(self.bars) else pd.concat([self.bars, new], ignore_index=True)
            self._save()
        return new

    def missing_opens(self) -> list[int]:
        """Open times absent between the first and last stored bar (the venue returned nothing for them)."""
        if len(self.bars) < 2:
            return []
        opens = to_epoch(self.bars["bar_open_time"])
        expected = range(int(opens[0]), int(opens[-1]) + self.step, self.step)
        return sorted(set(expected) - set(opens.tolist()))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        self.bars.to_parquet(tmp, index=False)
        tmp.replace(self.path)


def snapshot_seed(paths: Paths, product: str, granularity: str) -> pd.DataFrame | None:
    """Bars of the most recent frozen dataset for this product and granularity, if any."""
    found = [m for m in list_datasets(paths)
             if m.get("instrument") == product and m.get("granularity") == granularity]
    if not found:
        return None
    latest = max(found, key=lambda m: m.get("requested_end") or 0)
    bars, _ = load_dataset(paths, latest["dataset_id"])
    return bars
