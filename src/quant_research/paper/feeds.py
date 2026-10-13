"""Market data feeds for paper trading: snapshot replay and Coinbase public REST polling."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pandas as pd

from quant_research.data.bars import to_epoch
from quant_research.data.coinbase import SOURCE_VERSION, VENUE, CoinbasePublicClient
from quant_research.data.validate import validate_bars
from quant_research.paper.models import D
from quant_research.timeutil import granularity_seconds

SEED_BARS = 17_520  # two years of hourly bars: history to fetch when no frozen dataset can seed it


class ReplayFeed:
    """Replays completed bars from a frozen snapshot; each bar becomes available at its close."""

    def __init__(self, bars: pd.DataFrame):
        self.bars = bars.reset_index(drop=True)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        yield from self.bars.to_dict("records")


class CoinbaseCandlePoller:
    """Polls recent candles and emits each newly completed bar once, in order."""

    def __init__(self, client: CoinbasePublicClient, product_id: str, granularity: str, lookback_bars: int = 5,
                 clock: Callable[[], float] = time.time):
        self.client = client
        self.product_id = product_id
        self.granularity = granularity
        self.step = granularity_seconds(granularity)
        self.lookback = lookback_bars
        self.clock = clock
        self.last_open: int | None = None

    def _end(self) -> int:
        now = int(self.clock())
        return now - now % self.step

    def range(self, start: int, end: int) -> pd.DataFrame:
        """Completed bars with open time in [start, end), paged in 350-bar requests."""
        frames = []
        while start < end:
            we = min(start + 350 * self.step, end)
            frames.append(self._fetch(start, we))
            start = we
        frames = [f for f in frames if len(f)]
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        return df.drop_duplicates("bar_open_time").sort_values("bar_open_time").reset_index(drop=True)

    def history(self, bars: int) -> pd.DataFrame:
        """The most recent `bars` completed bars."""
        end = self._end()
        df = self.range(end - bars * self.step, end)
        if len(df):
            self.last_open = int(to_epoch(df["bar_open_time"])[-1])
        return df

    def _fetch(self, start: int, end: int) -> pd.DataFrame:
        rec = self.client.candles(self.product_id, start, end - self.step, self.granularity)
        fetched_at = self.clock()
        candles = rec.body.get("candles", [])
        if not candles:
            return pd.DataFrame()
        raw = pd.DataFrame([{"start": int(c["start"]), "open": float(c["open"]), "high": float(c["high"]),
                             "low": float(c["low"]), "close": float(c["close"]), "volume": float(c["volume"]),
                             "source_version": SOURCE_VERSION, "fetched_at": fetched_at}
                            for c in candles])
        bars, _ = validate_bars(raw, VENUE, self.product_id, self.granularity, start, end)
        # Live bars become available when we observe them, not at their nominal close.
        bars["available_at"] = pd.to_datetime(fetched_at, unit="s", utc=True)
        return bars

    def poll(self) -> list[dict[str, Any]]:
        """Every completed bar after the last one emitted, in order; after an outage this backfills the gap."""
        end = self._end()
        start = end - self.lookback * self.step if self.last_open is None else self.last_open + self.step
        bars = self.range(start, end) if start < end else pd.DataFrame()
        if len(bars) and self.last_open is not None:
            bars = bars[to_epoch(bars["bar_open_time"]) > self.last_open]
        if not len(bars):
            return []
        self.last_open = int(to_epoch(bars["bar_open_time"])[-1])
        return bars.to_dict("records")


def coinbase_mid_price(client: CoinbasePublicClient, product_id: str) -> Decimal:
    body = client.ticker(product_id).body
    bid, ask = body.get("best_bid"), body.get("best_ask")
    if bid and ask:
        return (D(bid) + D(ask)) / 2
    trades = body.get("trades") or []
    if not trades:
        raise RuntimeError("ticker returned neither best bid/ask nor trades")
    return D(trades[0]["price"])
