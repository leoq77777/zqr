"""The only entry point that reads holdout data. Requires a frozen candidate and records every viewing."""

from __future__ import annotations

import pandas as pd

from quant_research.data.snapshot import content_hash, load_manifest
from quant_research.experiments.gatekeeper import GateContext
from quant_research.experiments.runner import ExperimentRequest, ExperimentResult, run_experiment
from quant_research.experiments.store import ExperimentStore
from quant_research.paths import Paths
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.validation.splits import SegmentData, load_split


class HoldoutError(RuntimeError):
    pass


def load_holdout_data(paths: Paths, split_id: str) -> SegmentData:
    split = load_split(paths, split_id)
    bars = pd.read_parquet(paths.holdout / split_id / "full.parquet")
    if content_hash(bars) != load_manifest(paths, split.dataset_id)["data_hash"]:
        raise HoldoutError("holdout copy does not match the frozen dataset hash")
    return SegmentData(bars, split.holdout, "holdout")


def run_holdout(
    paths: Paths,
    store: ExperimentStore,
    request: ExperimentRequest,
    sandbox: SandboxConfig | None = None,
) -> tuple[ExperimentResult, int]:
    """Returns the result and how many holdout runs had already been made on this split."""
    spec = request.spec.normalized()
    if not store.is_frozen(spec.candidate_hash()):
        raise HoldoutError("candidate is not frozen; run `quant freeze-strategy` before the holdout test")
    data = load_holdout_data(paths, request.split_id)
    result = run_experiment(paths, store, request.model_copy(update={"parameters": spec.parameters}),
                            kind="holdout", data=data, sandbox=sandbox, gate=GateContext(caller="holdout"))
    prior = store.record_holdout_run(request.split_id, spec.candidate_hash(), result.experiment_id)
    note = ("first holdout viewing on this split" if prior == 0 else
            f"holdout already viewed {prior} time(s) on this split; result is no longer an untouched test")
    store.set_notes(result.experiment_id, note)
    return result, prior
