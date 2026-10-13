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


DEFAULT_MAX_VIEWINGS = 10  # per split, when no mandate on the split declares a limit


def split_viewing_cap(store: ExperimentStore, split_id: str) -> int:
    """The tightest viewing limit any mandate on this split declares; DEFAULT_MAX_VIEWINGS if none does."""
    from quant_research.research.mandate import Mandate

    caps = [Mandate.model_validate(m["body"]).holdout.max_viewings for m in store.list_mandates()
            if m["body"].get("split_id") == split_id]
    return min(caps) if caps else DEFAULT_MAX_VIEWINGS


def run_holdout(
    paths: Paths,
    store: ExperimentStore,
    request: ExperimentRequest,
    sandbox: SandboxConfig | None = None,
    *,
    max_viewings: int,
    override_reason: str | None = None,
) -> tuple[ExperimentResult, int]:
    """The single gate to holdout data; returns the result and the viewings recorded before this candidate's.

    A candidate already viewed on the split gets its recorded result back without a new viewing. Otherwise the
    split must have fewer than `max_viewings` viewings, unless `override_reason` is given; an override is recorded
    and marks the split's holdout as contaminated.
    """
    spec = request.spec.normalized()
    chash = spec.candidate_hash()
    if not store.is_frozen(chash):
        raise HoldoutError("candidate is not frozen; run `zqr freeze-strategy` before the holdout test")
    runs = store.holdout_runs(request.split_id)
    earlier = next((r for r in runs if r["candidate_hash"] == chash), None)
    over = len(runs) >= max_viewings
    if earlier is None and over and not override_reason:
        raise HoldoutError(f"holdout of {request.split_id} already viewed {len(runs)} time(s); the limit is "
                           f"{max_viewings}")
    data = load_holdout_data(paths, request.split_id)
    result = run_experiment(paths, store, request.model_copy(update={"parameters": spec.parameters}),
                            kind="holdout", data=data, sandbox=sandbox, gate=GateContext(caller="holdout"))
    if earlier is not None and result.status == "duplicate":
        store.set_notes(result.experiment_id, f"repeat of holdout run {earlier['experiment_id']}; not a new viewing")
        return result, earlier["prior_runs_on_split"]
    prior = store.record_holdout_run(request.split_id, chash, result.experiment_id)
    if over:
        reason = override_reason or "candidate re-run with a different request after the limit"
        store.record_holdout_override(request.split_id, result.experiment_id, reason)
        note = f"over the viewing limit ({reason}); this split's holdout is contaminated"
    elif prior == 0:
        note = "first holdout viewing on this split"
    else:
        note = f"holdout already viewed {prior} time(s) on this split; result is no longer an untouched test"
    store.set_notes(result.experiment_id, note)
    return result, prior


def run_candidate_holdout(paths: Paths, store: ExperimentStore, candidate_id: str,
                          sandbox: SandboxConfig | None = None) -> dict:
    """frozen -> final_evaluated | failed_final under the candidate's mandate (viewing limit and pass rule)."""
    from quant_research.research.mandate import Mandate, metric_value
    from quant_research.research.registry import transition

    cand = store.get_candidate(candidate_id)
    if cand is None:
        raise HoldoutError(f"unknown candidate {candidate_id}")
    if cand["state"] != "frozen":
        raise HoldoutError(f"only frozen candidates can be evaluated on the holdout (state {cand['state']})")
    rec = store.get_mandate(cand["mandate_id"]) if cand["mandate_id"] else None
    if rec is None:
        raise HoldoutError("candidate has no mandate; the holdout pass rule must be predeclared")
    mandate = Mandate.model_validate(rec["body"])
    if mandate.evaluation_design.kind != "single_validation":
        raise HoldoutError("holdout of a walk-forward procedure (re-selection through the holdout) is not "
                           "implemented; a fixed-parameter holdout would test a different candidate")
    request = ExperimentRequest(strategy_ref=cand["strategy_ref"], parameters=cand["parameters"],
                                dataset_id=mandate.dataset_id, split_id=mandate.split_id,
                                engine_config=mandate.engine_config, policy=mandate.policy)
    result, prior = run_holdout(paths, store, request, sandbox, max_viewings=mandate.holdout.max_viewings)
    evidence = {"holdout_experiment": result.experiment_id, "prior_viewings": prior, "status": result.status}
    if result.has_result:
        primary = metric_value(result.metrics, mandate.primary_metric)
        passed = (primary >= mandate.holdout.min_primary
                  and result.metrics["total_return"] >= mandate.holdout.min_total_return)
        evidence |= {"primary": primary, "total_return": result.metrics["total_return"],
                     "rule": mandate.holdout.model_dump()}
    else:
        passed = False
    cand = transition(store, candidate_id, "final_evaluated" if passed else "failed_final", "evaluator", evidence)
    return {"candidate": cand, "result": result, "prior": prior, "passed": passed}
