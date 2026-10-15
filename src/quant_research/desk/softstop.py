"""Soft stop (investment policy 2, decision D2): on monthly review days, once the combined paper account has twelve
months of daily returns, compare its trailing-365-day Sharpe (A4 formula) with buy and hold's on the same capital.
A difference below -0.5 writes `desk/soft_stop.json`; while it exists the research loop pauses for human review.
Paper trading continues. `clear` needs a note and keeps the record as `soft_stop.cleared-<ts>.json`."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pandas as pd

from quant_research.evaluation.split_stats import sharpe_daily
from quant_research.paths import Paths

THRESHOLD = -0.5
WINDOW_DAYS = 365


def soft_stop_path(paths: Paths) -> Path:
    return paths.desk / "soft_stop.json"


def active(paths: Paths) -> dict[str, Any] | None:
    p = soft_stop_path(paths)
    return json.loads(p.read_text()) if p.exists() else None


def evaluate(paths: Paths, day: str, books=None) -> dict[str, Any]:
    """The rule on the daily combined and benchmark returns of the 365 days before `day` (a review day)."""
    from quant_research.desk.portfolio_report import combined, daily, load_books

    if (current := active(paths)) is not None:
        return {"status": "active", **current}
    comb = combined(load_books(paths) if books is None else books)
    end = pd.Timestamp(day, tz="UTC")
    if not len(comb):
        return {"status": "too early", "reason": "no portfolio in paper"}
    eq = daily(comb["combined"])
    bench = daily(comb["benchmark"])
    eq, bench = eq[eq.index < end], bench[bench.index < end]
    r_eq, r_bench = eq.pct_change().dropna(), bench.pct_change().dropna()
    if len(r_eq) < WINDOW_DAYS:
        return {"status": "too early", "reason": f"{len(r_eq)} days of combined paper returns, need {WINDOW_DAYS}"}
    r_eq, r_bench = r_eq.iloc[-WINDOW_DAYS:], r_bench.iloc[-WINDOW_DAYS:]
    s_eq, s_bench = sharpe_daily(r_eq.to_numpy()), sharpe_daily(r_bench.to_numpy())
    out = {"combined_sharpe": round(s_eq, 4), "benchmark_sharpe": round(s_bench, 4),
           "difference": round(s_eq - s_bench, 4), "threshold": THRESHOLD,
           "window": [str(r_eq.index[0].date()), str(r_eq.index[-1].date())], "review_day": day}
    if s_eq - s_bench < THRESHOLD:
        record = {**out, "triggered_at": time.time()}
        soft_stop_path(paths).parent.mkdir(parents=True, exist_ok=True)
        soft_stop_path(paths).write_text(json.dumps(record, indent=2))
        return {"status": "triggered", **record}
    return {"status": "ok", **out}


def clear(paths: Paths, note: str) -> dict[str, Any]:
    if not note.strip():
        raise ValueError("clearing the soft stop needs a note")
    current = active(paths)
    if current is None:
        raise ValueError("no soft stop is active")
    now = time.time()
    record = {**current, "cleared_at": now, "note": note}
    dest = paths.desk / f"soft_stop.cleared-{int(now)}.json"
    dest.write_text(json.dumps(record, indent=2))
    soft_stop_path(paths).unlink()
    return record
