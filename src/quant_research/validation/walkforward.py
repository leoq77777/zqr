"""Rolling walk-forward: select parameters on each train window, score them on the next test window.

The procedure (grid + windows) is one research trial (kind `walkforward`); its inner fold runs are recorded as
`walkforward_fold` and not counted again. Out-of-sample metrics come from the concatenated test windows.
"""

from __future__ import annotations

import itertools
import math
from typing import Any, Literal

import numpy as np
import pandas as pd

from quant_research import ENGINE_VERSION
from quant_research.data.bars import to_epoch
from quant_research.evaluation.statistics import deflate, sharpe_statistics
from quant_research.experiments.runner import ExperimentRequest, run_experiment
from quant_research.experiments.store import ExperimentStore
from quant_research.paths import Paths
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.timeutil import periods_per_year
from quant_research.validation.splits import SegmentData, load_research_segment, load_split


def expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(grid)
    return [dict(zip(keys, values, strict=True)) for values in itertools.product(*(grid[k] for k in keys))]


def _oos_metrics(equity: list[pd.DataFrame], trips: int, initial_cash: float, ppy: float) -> tuple[dict, np.ndarray]:
    rets = np.concatenate([e["equity"].pct_change().fillna(e["equity"].iloc[0] / initial_cash - 1).to_numpy()
                           for e in equity])
    curve = np.concatenate([[1.0], np.cumprod(1 + rets)])
    std = rets.std(ddof=1) if len(rets) > 1 else 0.0
    total = float(curve[-1] - 1)
    years = len(rets) / ppy
    cagr = (curve[-1]) ** (1 / years) - 1 if curve[-1] > 0 and years > 0 else -1.0
    max_dd = float((curve / np.maximum.accumulate(curve) - 1).min())
    sharpe = float(rets.mean() / std * math.sqrt(ppy)) if std > 0 else 0.0
    exposure = float(np.concatenate([e["exposure"].to_numpy() for e in equity]).mean())
    return {"total_return": total, "cagr": float(cagr), "sharpe": sharpe, "max_drawdown": max_dd,
            "calmar": float(cagr / abs(max_dd)) if max_dd < 0 else 0.0, "n_round_trips": trips,
            "exposure": exposure, "bars": int(len(rets)), "periods_per_year": ppy}, rets


def walk_forward(
    paths: Paths,
    store: ExperimentStore,
    base: ExperimentRequest,
    grid: dict[str, list[Any]],
    train_bars: int,
    test_bars: int,
    *,
    through: Literal["dev", "validation"] = "dev",
    metric: str = "sharpe",
    session_id: str | None = None,
    sandbox: SandboxConfig | None = None,
    kind: str = "walkforward",
    parent_id: str | None = None,
    gate=None,
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    seg = load_research_segment(paths, base.split_id, through)
    split = load_split(paths, base.split_id)
    bars = seg.bars
    opens = to_epoch(bars["bar_open_time"])
    usable = opens[(opens >= split.dev[0]) & (opens < seg.window[1])]
    candidates = expand_grid(grid)
    store.record_grid(base.strategy_ref, grid, len(candidates), session_id)
    parent = store.start_experiment(kind=kind, session_id=session_id, parent_id=parent_id,
                                    strategy_ref=base.strategy_ref,
                                    parameters={"grid": grid, "train_bars": train_bars, "test_bars": test_bars,
                                                "metric": metric},
                                    dataset_id=base.dataset_id, split_id=base.split_id, segment=f"wf:{through}",
                                    engine_config_id=base.engine_config.config_id(),
                                    engine_config=base.engine_config.model_dump(), engine_version=ENGINE_VERSION,
                                    policy_id=base.policy.policy_id(), policy=base.policy.model_dump(),
                                    **(record or {}))
    fold_kw = {"kind": "walkforward_fold", "parent_id": parent, "session_id": session_id, "sandbox": sandbox,
               "gate": gate}
    folds = []
    oos_equity: list[pd.DataFrame] = []
    oos_trips = 0
    k, start = 0, 0
    while start + train_bars + test_bars <= len(usable):
        train_w = (int(usable[start]), int(usable[start + train_bars]))
        test_end_idx = start + train_bars + test_bars
        test_w = (train_w[1], int(usable[test_end_idx]) if test_end_idx < len(usable) else seg.window[1])
        best, best_score = None, -math.inf
        for params in candidates:
            data = SegmentData(bars[opens < train_w[1]].reset_index(drop=True), train_w, f"{through}:wf{k}:train")
            res = run_experiment(paths, store, base.model_copy(update={"parameters": params}), data=data, **fold_kw)
            if res.metrics and res.metrics[metric] > best_score:
                best, best_score = params, res.metrics[metric]
        fold = {"fold": k, "train": train_w, "test": test_w, "selected": best, "train_score": best_score}
        if best is not None:
            data = SegmentData(bars[opens < test_w[1]].reset_index(drop=True), test_w, f"{through}:wf{k}:test")
            res = run_experiment(paths, store, base.model_copy(update={"parameters": best}), data=data, **fold_kw)
            fold.update(test_status=res.status, test_metrics=res.metrics, test_experiment=res.experiment_id)
            if res.result_dir:
                oos_equity.append(pd.read_parquet(f"{res.result_dir}/equity.parquet"))
                oos_trips += int(res.metrics["n_round_trips"])
        folds.append(fold)
        k += 1
        start += test_bars

    summary: dict[str, Any] = {"parent_experiment": parent, "folds": folds, "n_candidates": len(candidates)}
    if not oos_equity:
        store.finish_experiment(parent, "failed", failure_reason="no completed out-of-sample fold",
                                checks={"folds": len(folds)})
        return summary
    ppy = periods_per_year(split.granularity)
    metrics, rets = _oos_metrics(oos_equity, oos_trips, base.engine_config.initial_cash, ppy)
    trials = max(store.split_trial_count(base.split_id)["total"], 1)
    stats = deflate(sharpe_statistics(rets, ppy), trials, store.research_sharpes(base.split_id), ppy)
    summary |= {f"oos_{k}": v for k, v in metrics.items()} | {"metrics": metrics, "statistics": stats}
    store.finish_experiment(parent, "completed", metrics=metrics, statistics=stats,
                            checks={"folds": len(folds), "selected": [f["selected"] for f in folds]})
    return summary
