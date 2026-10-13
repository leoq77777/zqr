"""Rolling walk-forward: select parameters on each train window, score them on the next test window."""

from __future__ import annotations

import itertools
import math
from typing import Any, Literal

import numpy as np
import pandas as pd

from quant_research.data.bars import to_epoch
from quant_research.experiments.runner import ExperimentRequest, run_experiment
from quant_research.experiments.store import ExperimentStore
from quant_research.paths import Paths
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.timeutil import periods_per_year
from quant_research.validation.splits import SegmentData, load_research_segment, load_split


def expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(grid)
    return [dict(zip(keys, values, strict=True)) for values in itertools.product(*(grid[k] for k in keys))]


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
) -> dict[str, Any]:
    seg = load_research_segment(paths, base.split_id, through)
    split = load_split(paths, base.split_id)
    bars = seg.bars
    opens = to_epoch(bars["bar_open_time"])
    usable = opens[(opens >= split.dev[0]) & (opens < seg.window[1])]
    candidates = expand_grid(grid)
    store.record_grid(base.strategy_ref, grid, len(candidates), session_id)
    parent = store.start_experiment(kind="walkforward", session_id=session_id, strategy_ref=base.strategy_ref,
                                    parameters={"grid": grid, "train_bars": train_bars, "test_bars": test_bars,
                                                "metric": metric},
                                    dataset_id=base.dataset_id, split_id=base.split_id, segment=f"wf:{through}",
                                    engine_config_id=base.engine_config.config_id(),
                                    engine_config=base.engine_config.model_dump())
    folds = []
    oos_equity: list[pd.DataFrame] = []
    k, start = 0, 0
    while start + train_bars + test_bars <= len(usable):
        train_w = (int(usable[start]), int(usable[start + train_bars]))
        test_end_idx = start + train_bars + test_bars
        test_w = (train_w[1], int(usable[test_end_idx]) if test_end_idx < len(usable) else seg.window[1])
        best, best_score = None, -math.inf
        for params in candidates:
            data = SegmentData(bars[opens < train_w[1]].reset_index(drop=True), train_w, f"wf{k}:train")
            res = run_experiment(paths, store, base.model_copy(update={"parameters": params}), kind="walkforward",
                                 parent_id=parent, session_id=session_id, data=data, sandbox=sandbox)
            if res.metrics and res.metrics[metric] > best_score:
                best, best_score = params, res.metrics[metric]
        fold = {"fold": k, "train": train_w, "test": test_w, "selected": best, "train_score": best_score}
        if best is not None:
            data = SegmentData(bars[opens < test_w[1]].reset_index(drop=True), test_w, f"wf{k}:test")
            res = run_experiment(paths, store, base.model_copy(update={"parameters": best}), kind="walkforward",
                                 parent_id=parent, session_id=session_id, data=data, sandbox=sandbox)
            fold.update(test_status=res.status, test_metrics=res.metrics)
            if res.result_dir:
                oos_equity.append(pd.read_parquet(f"{res.result_dir}/equity.parquet"))
        folds.append(fold)
        k += 1
        start += test_bars

    summary: dict[str, Any] = {"parent_experiment": parent, "folds": folds, "n_candidates": len(candidates)}
    if oos_equity:
        rets = np.concatenate([e["equity"].pct_change().fillna(
            e["equity"].iloc[0] / base.engine_config.initial_cash - 1).to_numpy() for e in oos_equity])
        ppy = periods_per_year(split.granularity)
        std = rets.std(ddof=1) if len(rets) > 1 else 0.0
        summary["oos_total_return"] = float(np.prod(1 + rets) - 1)
        summary["oos_sharpe"] = float(rets.mean() / std * math.sqrt(ppy)) if std > 0 else 0.0
        summary["oos_bars"] = int(len(rets))
    store.finish_experiment(parent, "completed", metrics={k: v for k, v in summary.items() if k.startswith("oos_")},
                            checks={"folds": len(folds)})
    return summary
