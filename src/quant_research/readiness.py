"""Venue readiness table: documented -> network-measured -> account -> sandbox -> production read-only."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

import numpy as np

from quant_research.data.coinbase import CoinbaseError, CoinbasePublicClient
from quant_research.timeutil import granularity_seconds, to_iso

CHECKS = ["docs_confirmed", "network_measured", "account_confirmed", "sandbox_verified", "production_readonly_verified"]
Status = Literal["pending", "pass", "fail", "n/a"]

DEFAULT = {
    "coinbase": {
        "docs_confirmed": {"status": "pass", "evidence": "development-plan.md section 4 (official docs links)"},
        "network_measured": {"status": "pending"},
        "account_confirmed": {"status": "pending", "evidence": "account region/KYC/API permissions not confirmed"},
        "sandbox_verified": {"status": "n/a", "evidence": "Coinbase sandbox returns static responses only"},
        "production_readonly_verified": {"status": "pending"},
    },
}


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else json.loads(json.dumps(DEFAULT))


def set_check(path: Path, venue: str, check: str, status: Status, evidence: str, data: Any = None) -> dict:
    if check not in CHECKS:
        raise ValueError(f"check must be one of {CHECKS}")
    table = load(path)
    table.setdefault(venue, {})[check] = {"status": status, "evidence": evidence, "at": time.time(),
                                          "at_iso": to_iso(time.time()), "data": data}
    path.write_text(json.dumps(table, indent=2))
    return table


def probe_rest(client: CoinbasePublicClient, product_id: str, n: int = 20) -> dict[str, Any]:
    """Latency/error profile of the public candles endpoint from this machine's network."""
    latencies, errors = [], []
    now = int(time.time())
    step = granularity_seconds("ONE_HOUR")
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            rec = client.candles(product_id, now - 24 * step, now - step, "ONE_HOUR")
            latencies.append(rec.latency_s)
        except CoinbaseError as exc:
            errors.append(str(exc)[:200])
            latencies.append(time.perf_counter() - t0)
    lat = np.array(latencies) * 1000
    return {"product_id": product_id, "requests": n, "errors": len(errors), "error_samples": errors[:3],
            "p50_ms": float(np.percentile(lat, 50)), "p95_ms": float(np.percentile(lat, 95)),
            "p99_ms": float(np.percentile(lat, 99)), "measured_at": to_iso(time.time())}


def probe_coverage(client: CoinbasePublicClient, product_id: str, granularity: str, start: int) -> dict[str, Any]:
    """Does the venue return bars at the requested history start? (sampled, not full coverage)."""
    step = granularity_seconds(granularity)
    rec = client.candles(product_id, start, start + 349 * step, granularity)
    starts = sorted(int(c["start"]) for c in rec.body.get("candles", []))
    return {"product_id": product_id, "granularity": granularity, "requested_start": to_iso(start),
            "bars_in_first_window": len(starts), "first_bar": to_iso(starts[0]) if starts else None,
            "last_bar": to_iso(starts[-1]) if starts else None}
