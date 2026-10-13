"""Offline re-execution of a recorded experiment from its stored identity."""

from __future__ import annotations

from typing import Any

import pandas as pd

from quant_research.backtest.engine import EngineConfig
from quant_research.data.bars import to_epoch
from quant_research.data.snapshot import load_manifest
from quant_research.experiments.runner import ExperimentRequest, compute_experiment
from quant_research.experiments.store import ExperimentStore
from quant_research.paths import Paths
from quant_research.portfolio.policy import PortfolioPolicy
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.validation.splits import SegmentData


class ReplayError(RuntimeError):
    pass


def _round(d: dict[str, Any]) -> dict[str, Any]:
    return {k: round(v, 10) if isinstance(v, float) else v for k, v in d.items()}


def replay_experiment(paths: Paths, store: ExperimentStore, exp_id: str,
                      sandbox: SandboxConfig | None = None) -> dict[str, Any]:
    rec = store.get_experiment(exp_id)
    if rec is None:
        raise ReplayError(f"unknown experiment {exp_id}")
    if rec["status"] not in ("completed", "duplicate"):
        raise ReplayError(f"experiment {exp_id} has status {rec['status']}; nothing to reproduce")
    manifest = load_manifest(paths, rec["dataset_id"])
    if manifest["data_hash"] != rec["data_hash"]:
        raise ReplayError("dataset content changed since the experiment was recorded")
    source = (paths.holdout / rec["split_id"] / "full.parquet" if rec["kind"] == "holdout"
              else paths.splits / rec["split_id"] / "research.parquet")
    bars = pd.read_parquet(source)
    window = (rec["window_start"], rec["window_end"])
    bars = bars[to_epoch(bars["bar_open_time"]) < window[1]].reset_index(drop=True)
    segment = rec["segment"] if rec["segment"] in ("dev", "validation") else "dev"
    request = ExperimentRequest(strategy_ref=rec["strategy_ref"], parameters=rec["parameters"],
                                dataset_id=rec["dataset_id"], split_id=rec["split_id"], segment=segment,
                                engine_config=EngineConfig.model_validate(rec["engine_config"]),
                                policy=PortfolioPolicy.model_validate(rec.get("policy") or {}),
                                allow_gaps=bool(rec["checks"] and rec["checks"].get("gaps_in_history")))
    computed = compute_experiment(paths, request, sandbox, SegmentData(bars, window, rec["segment"]))
    # Metrics added by later engine versions are not part of an older record.
    original = _round(rec["metrics"])
    again = {k: v for k, v in _round(computed["metrics"]).items() if k in original}
    if rec["statistics"]:
        # DSR fields depend on the trial count at recording time; only trial-independent statistics replay.
        again |= {f"statistics.{k}": v for k, v in _round(computed["statistics"]).items()}
        original |= {f"statistics.{k}": v for k, v in _round(rec["statistics"]).items()
                     if k in computed["statistics"]}
    diffs = {k: (original.get(k), again.get(k)) for k in set(original) | set(again) if original.get(k) != again.get(k)}
    return {"experiment_id": exp_id, "reproducible": not diffs, "differences": diffs,
            "engine_version_recorded": rec["engine_version"], "metrics": computed["metrics"]}
