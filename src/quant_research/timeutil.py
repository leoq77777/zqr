from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

GRANULARITY_SECONDS: dict[str, int] = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 300,
    "FIFTEEN_MINUTE": 900,
    "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600,
    "TWO_HOUR": 7200,
    "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
}

_ALIASES = {
    "1m": "ONE_MINUTE",
    "5m": "FIVE_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "1h": "ONE_HOUR",
    "2h": "TWO_HOUR",
    "6h": "SIX_HOUR",
    "1d": "ONE_DAY",
}

SECONDS_PER_YEAR = 365 * 86400  # crypto trades 24/7


def parse_granularity(value: str) -> str:
    key = _ALIASES.get(value.lower(), value.upper())
    if key not in GRANULARITY_SECONDS:
        raise ValueError(f"unsupported granularity {value!r}; choose from {sorted(GRANULARITY_SECONDS)}")
    return key


def granularity_seconds(granularity: str) -> int:
    return GRANULARITY_SECONDS[parse_granularity(granularity)]


def periods_per_year(granularity: str) -> float:
    return SECONDS_PER_YEAR / granularity_seconds(granularity)


def parse_time(value: str | int | float) -> int:
    """Parse epoch seconds, YYYY-MM-DD or ISO-8601 into UTC epoch seconds. Naive inputs are UTC."""
    if isinstance(value, (int, float)):
        return int(value)
    text = value.strip()
    if text.isdigit():
        return int(text)
    ts = pd.Timestamp(text)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.tz_convert("UTC").timestamp())


def to_iso(ts: int | float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def floor_to(ts: int, step: int) -> int:
    return ts - ts % step


def ceil_to(ts: int, step: int) -> int:
    return -(-ts // step) * step
