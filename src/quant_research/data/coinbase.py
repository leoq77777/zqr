"""Coinbase Advanced Trade public market data (no API key)."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from quant_research.timeutil import ceil_to, granularity_seconds, parse_granularity

log = logging.getLogger(__name__)

VENUE = "coinbase"
BASE_URL = "https://api.coinbase.com/api/v3/brokerage"
SOURCE_VERSION = "coinbase-advanced-trade-v3/market/products/candles"
MAX_CANDLES_PER_REQUEST = 350
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """Process-wide minimum spacing between requests; share one instance across all callers."""

    def __init__(self, requests_per_second: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = 1.0 / requests_per_second
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            wait = self._next - now
            self._next = max(now, self._next) + self.interval
        if wait > 0:
            self._sleep(wait)


class CoinbaseError(RuntimeError):
    pass


@dataclass
class ResponseRecord:
    status: int
    body: Any
    latency_s: float
    attempts: int
    url: str
    params: dict[str, Any]


class CoinbasePublicClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        limiter: RateLimiter | None = None,
        http: httpx.Client | None = None,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        timeout: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.limiter = limiter or RateLimiter(5.0)
        self.http = http or httpx.Client(timeout=timeout, headers={"User-Agent": "quant-research/0.1"})
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep

    def get(self, path: str, params: dict[str, Any] | None = None) -> ResponseRecord:
        url = f"{self.base_url}{path}"
        params = params or {}
        last_error: str | None = None
        for attempt in range(1, self.max_retries + 2):
            self.limiter.acquire()
            t0 = time.perf_counter()
            try:
                resp = self.http.get(url, params=params)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("transport error on %s (attempt %d): %s", path, attempt, last_error)
                self._backoff(attempt, None)
                continue
            latency = time.perf_counter() - t0
            if resp.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {resp.status_code}"
                log.warning("retryable %s on %s (attempt %d)", last_error, path, attempt)
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            if resp.status_code != 200:
                raise CoinbaseError(f"GET {path} {params} -> HTTP {resp.status_code}: {resp.text[:300]}")
            return ResponseRecord(resp.status_code, resp.json(), latency, attempt, url, params)
        raise CoinbaseError(f"GET {path} failed after {self.max_retries + 1} attempts: {last_error}")

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if attempt > self.max_retries:
            return
        delay = self.backoff_base * 2 ** (attempt - 1)
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, float(retry_after))
        self._sleep(min(delay, 60.0))

    def candles(self, product_id: str, start: int, end: int, granularity: str) -> ResponseRecord:
        return self.get(
            f"/market/products/{product_id}/candles",
            {"start": str(start), "end": str(end), "granularity": parse_granularity(granularity)},
        )

    def product(self, product_id: str) -> ResponseRecord:
        return self.get(f"/market/products/{product_id}")

    def ticker(self, product_id: str, limit: int = 1) -> ResponseRecord:
        return self.get(f"/market/products/{product_id}/ticker", {"limit": limit})


def plan_windows(start: int, end: int, granularity: str) -> list[tuple[int, int]]:
    """Split [start, end) into grid-aligned windows of at most MAX_CANDLES_PER_REQUEST bars."""
    step = granularity_seconds(granularity)
    span = step * MAX_CANDLES_PER_REQUEST
    ws = ceil_to(start, step)
    windows = []
    while ws < end:
        we = min(ws + span, end)
        windows.append((ws, we))
        ws = we
    return windows


@dataclass
class FetchSummary:
    product_id: str
    granularity: str
    start: int
    end: int
    windows: int = 0
    fetched: int = 0
    resumed: int = 0
    candles: int = 0
    files: list[str] = field(default_factory=list)


def raw_dir_for(raw_root: Path, product_id: str, granularity: str) -> Path:
    return raw_root / VENUE / product_id / parse_granularity(granularity)


def fetch_range(
    client: CoinbasePublicClient,
    product_id: str,
    granularity: str,
    start: int,
    end: int,
    raw_root: Path,
    now: Callable[[], float] = time.time,
) -> FetchSummary:
    """Download [start, end) window by window, persisting each raw response.

    A window already on disk is reused only if it was fetched after the window fully closed;
    windows touching the in-progress period are always re-fetched.
    """
    granularity = parse_granularity(granularity)
    step = granularity_seconds(granularity)
    out_dir = raw_dir_for(raw_root, product_id, granularity)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "requests.jsonl"
    summary = FetchSummary(product_id, granularity, start, end)

    for ws, we in plan_windows(start, end, granularity):
        summary.windows += 1
        path = out_dir / f"{ws}_{we}.json"
        if path.exists():
            existing = json.loads(path.read_text())
            if existing.get("status") == 200 and existing.get("fetched_at", 0) >= we:
                summary.resumed += 1
                summary.candles += len(existing["body"].get("candles", []))
                summary.files.append(path.name)
                continue
        # Coinbase treats `end` as inclusive, so request through the last bar start in the window.
        record = client.candles(product_id, ws, we - step, granularity)
        fetched_at = now()
        payload = {
            "venue": VENUE,
            "product_id": product_id,
            "granularity": granularity,
            "window": [ws, we],
            "request": {"url": record.url, "params": record.params},
            "status": record.status,
            "attempts": record.attempts,
            "latency_s": record.latency_s,
            "fetched_at": fetched_at,
            "source_version": SOURCE_VERSION,
            "body": record.body,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
        n = len(record.body.get("candles", []))
        with open(log_path, "a") as f:
            f.write(json.dumps({"fetched_at": fetched_at, "window": [ws, we], "status": record.status,
                                "attempts": record.attempts, "latency_s": record.latency_s,
                                "candles": n}) + "\n")
        summary.fetched += 1
        summary.candles += n
        summary.files.append(path.name)
    return summary
