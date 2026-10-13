"""Robustness checks: fixed sensitivity cases (costs x2, delay, delay + adverse fill), neighbouring parameters,
sub-period stability."""

from __future__ import annotations

from typing import Any

import numpy as np

from quant_research.data.bars import to_epoch
from quant_research.experiments.runner import ExperimentRequest, run_experiment
from quant_research.experiments.store import ExperimentStore
from quant_research.paths import Paths
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.strategy.templates import get_template
from quant_research.validation.splits import SegmentData, load_research_segment


def neighbor_parameters(template_name: str, params: dict[str, Any], rel_step: float = 0.2) -> list[dict[str, Any]]:
    template = get_template(template_name)
    out = []
    for key, value in params.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if isinstance(value, int):
            delta = max(1, round(abs(value) * rel_step))
            candidates = [value - delta, value + delta]
        else:
            delta = abs(value) * rel_step or 0.01
            candidates = [value - delta, value + delta]
        for cand in candidates:
            trial = {**params, key: cand}
            try:
                out.append(template.validate(trial))
            except ValueError:
                continue
    return out


def run_robustness(
    paths: Paths,
    store: ExperimentStore,
    request: ExperimentRequest,
    *,
    parent_id: str | None = None,
    session_id: str | None = None,
    subperiods: int = 4,
    sandbox: SandboxConfig | None = None,
) -> dict[str, Any]:
    kw = {"kind": "robustness", "parent_id": parent_id, "session_id": session_id, "sandbox": sandbox}
    base = run_experiment(paths, store, request, **{**kw, "kind": "backtest"}) if parent_id is None else None
    parent = parent_id or base.experiment_id
    kw["parent_id"] = parent
    base_metrics = base.metrics if base else store.get_experiment(parent)["metrics"]

    sensitivity = {}
    for case in ("costs_x2", "delay_1", "delay_1_adverse"):
        res = run_experiment(paths, store, request.model_copy(
            update={"engine_config": request.engine_config.sensitivity(case)}), **kw)
        sensitivity[case] = {"status": res.status, "metrics": res.metrics, "experiment_id": res.experiment_id}
    cost2 = sensitivity["costs_x2"]

    neighbors = []
    if request.strategy_ref.startswith("template:"):
        for p in neighbor_parameters(request.strategy_ref.split(":", 1)[1], request.parameters):
            res = run_experiment(paths, store, request.model_copy(update={"parameters": p}), **kw)
            neighbors.append({"parameters": p, "status": res.status,
                              "sharpe": res.metrics["sharpe"] if res.metrics else None,
                              "total_return": res.metrics["total_return"] if res.metrics else None})

    seg = load_research_segment(paths, request.split_id, request.segment)
    opens = to_epoch(seg.bars["bar_open_time"])
    in_win = opens[(opens >= seg.window[0]) & (opens < seg.window[1])]
    subs = []
    for i, chunk in enumerate(np.array_split(in_win, subperiods)):
        if not len(chunk):
            continue
        window = (int(chunk[0]), int(chunk[-1]) + 1)
        data = SegmentData(seg.bars[opens < window[1]].reset_index(drop=True), window, f"{request.segment}:sub{i}")
        res = run_experiment(paths, store, request, data=data, **kw)
        subs.append({"window": window, "status": res.status,
                     "total_return": res.metrics["total_return"] if res.metrics else None,
                     "sharpe": res.metrics["sharpe"] if res.metrics else None,
                     "buy_and_hold_return": res.benchmarks["buy_and_hold"]["total_return"] if res.benchmarks
                     else None})

    neighbor_sharpes = [n["sharpe"] for n in neighbors if n["sharpe"] is not None]
    sub_returns = [s["total_return"] for s in subs if s["total_return"] is not None]
    return {
        "parent_experiment": parent,
        "base": base_metrics,
        "cost_doubled": cost2["metrics"],
        "survives_cost_doubling": bool(cost2["metrics"] and cost2["metrics"]["total_return"] > 0),
        "sensitivity": sensitivity,
        "survives_delay": bool(sensitivity["delay_1_adverse"]["metrics"]
                               and sensitivity["delay_1_adverse"]["metrics"]["total_return"] > 0),
        "neighbors": neighbors,
        "neighbor_sharpe_min": min(neighbor_sharpes) if neighbor_sharpes else None,
        "neighbor_sharpe_median": float(np.median(neighbor_sharpes)) if neighbor_sharpes else None,
        "subperiods": subs,
        "subperiod_positive_fraction": (sum(r > 0 for r in sub_returns) / len(sub_returns)) if sub_returns else None,
    }
