"""P5 trigger measurement: fixed workload, wall time, peak memory, and serial/parallel result equality."""

from __future__ import annotations

import platform
import resource
import sys
import time
from typing import Any

import numpy as np
import pandas as pd

from quant_research.backtest.engine import EngineConfig, run_backtest
from quant_research.data.bars import to_utc
from quant_research.evaluation.metrics import compute_metrics
from quant_research.hashing import hash_obj
from quant_research.strategy.compute import compute_targets
from quant_research.strategy.spec import StrategySpec


def synthetic_bars(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(0.00005 + 0.01 * rng.standard_normal(n)))
    open_ = np.concatenate([[100.0], close[:-1]])
    return pd.DataFrame({"bar_open_time": to_utc(1609459200 + 3600 * np.arange(n)), "open": open_,
                         "high": np.maximum(open_, close), "low": np.minimum(open_, close), "close": close,
                         "volume": 1.0, "filled": False})


def _candidates(k: int) -> list[dict[str, int]]:
    out = []
    for fast in range(5, 100, 5):
        for slow in range(fast * 2, fast * 12, max(fast // 2, 5)):
            out.append({"fast": fast, "slow": slow})
            if len(out) == k:
                return out
    return out


def _one(args: tuple[pd.DataFrame, dict[str, int]]) -> dict[str, float]:
    bars, params = args
    spec = StrategySpec(strategy_ref="template:sma_cross", parameters=params)
    targets = compute_targets(spec, bars).targets
    cfg = EngineConfig()
    return compute_metrics(run_backtest(bars, targets, cfg), cfg.initial_cash, 8760)


def _peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / 1024 / 1024 if sys.platform == "darwin" else rss / 1024


def run_benchmark(assets: int = 2, years: float = 5, candidates: int = 100, workers: int = 1) -> dict[str, Any]:
    from concurrent.futures import ProcessPoolExecutor

    n = int(years * 8760)
    datasets = [synthetic_bars(n, seed) for seed in range(assets)]
    jobs = [(bars, p) for bars in datasets for p in _candidates(candidates)]
    t0 = time.perf_counter()
    serial = [_one(j) for j in jobs]
    t_serial = time.perf_counter() - t0
    result: dict[str, Any] = {
        "machine": {"platform": platform.platform(), "processor": platform.processor(), "python": sys.version},
        "workload": {"assets": assets, "bars_per_asset": n, "candidates_per_asset": len(jobs) // assets,
                     "backtests": len(jobs)},
        "serial_wall_s": t_serial, "serial_backtests_per_s": len(jobs) / t_serial,
        "peak_rss_mb": _peak_rss_mb(), "result_hash": hash_obj(serial),
    }
    if workers > 1:
        t0 = time.perf_counter()
        with ProcessPoolExecutor(max_workers=workers) as pool:
            parallel = list(pool.map(_one, jobs, chunksize=4))
        t_par = time.perf_counter() - t0
        result.update(parallel_workers=workers, parallel_wall_s=t_par, parallel_speedup=t_serial / t_par,
                      parallel_matches_serial=hash_obj(parallel) == result["result_hash"])
    return result
