"""The only entry point that reads holdout data. Requires a frozen candidate and records every viewing."""

from __future__ import annotations

import time
from collections.abc import Callable

import pandas as pd

from quant_research import ENGINE_VERSION
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
    """The tightest viewing limit any mandate on this split declares; DEFAULT_MAX_VIEWINGS if none does.

    Read from the raw bodies so mandates written under an older schema still count; malformed limits are skipped.
    """
    caps = []
    for m in store.list_mandates():
        body = m["body"]
        if not isinstance(body, dict) or body.get("split_id") != split_id:
            continue
        cap = (body.get("holdout") or {}).get("max_viewings")
        if type(cap) is int and cap > 0:
            caps.append(cap)
    return min(caps) if caps else DEFAULT_MAX_VIEWINGS


def _repeats_a_completed_run(paths: Paths, store: ExperimentStore, request: ExperimentRequest, chash: str) -> bool:
    """Whether the gatekeeper would answer this holdout request with an earlier identical result."""
    split = load_split(paths, request.split_id)
    if split.dataset_id != request.dataset_id:
        return False
    data_hash = load_manifest(paths, request.dataset_id)["data_hash"]
    return store.find_completed(chash, data_hash, request.split_id, split.holdout, request.engine_config.config_id(),
                                ENGINE_VERSION, request.policy.policy_id()) is not None


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

    The limit is the tighter of `max_viewings` and `split_viewing_cap`. A candidate already viewed on the split
    gets its recorded result back without a new viewing. Otherwise the split must be under the limit, unless
    `override_reason` is given; an override is recorded and marks the split's holdout as contaminated. Over the
    limit, only an exact repeat of a completed run proceeds without an override.
    """
    spec = request.spec.normalized()
    chash = spec.candidate_hash()
    if not store.is_frozen(chash):
        raise HoldoutError("candidate is not frozen; run `zqr freeze-strategy` before the holdout test")
    runs = store.holdout_runs(request.split_id)
    earlier = next((r for r in runs if r["candidate_hash"] == chash), None)
    cap = min(max_viewings, split_viewing_cap(store, request.split_id))
    over = len(runs) >= cap
    if over and not override_reason and not (earlier is not None
                                             and _repeats_a_completed_run(paths, store, request, chash)):
        raise HoldoutError(f"holdout of {request.split_id} already viewed {len(runs)} time(s); the limit is {cap}")
    data = load_holdout_data(paths, request.split_id)
    result = run_experiment(paths, store, request.model_copy(update={"parameters": spec.parameters}),
                            kind="holdout", data=data, sandbox=sandbox, gate=GateContext(caller="holdout"))
    if earlier is not None and result.status == "duplicate":
        store.set_notes(result.experiment_id, f"repeat of holdout run {earlier['experiment_id']}; not a new viewing")
        return result, earlier["prior_runs_on_split"]
    prior = store.record_holdout_run(request.split_id, chash, result.experiment_id)
    if over:
        store.record_holdout_override(request.split_id, result.experiment_id, override_reason)
        note = f"over the viewing limit ({override_reason}); this split's holdout is contaminated"
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


PORTFOLIO_GATE_3 = {"min_sharpe_daily": 0.5, "min_total_return": 0.0}  # total return must exceed it
VERSION_BATCH_SECONDS = 7 * 86_400


def run_portfolio_holdout(paths: Paths, store: ExperimentStore, version_id: str, override_reason: str | None = None,
                          now: Callable[[], float] = time.time) -> dict:
    """frozen -> final_evaluated | failed_final on gate 3 (A6), under the split's viewing cap and the A9 batching
    rule: at most one portfolio version per split every 7 days. A version already viewed gets its recorded result
    back without a new viewing. `override_reason` lifts the cap and the batching rule; it is recorded and marks the
    split's holdout as contaminated."""
    from quant_research.portfolio.evaluate import record_run
    from quant_research.portfolio.version import VersionStore

    vs = VersionStore(store)
    row = vs.get(version_id)
    if row is None:
        raise HoldoutError(f"unknown portfolio version {version_id}")
    version = vs.version(version_id)
    runs = store.holdout_runs(version.split_id)
    earlier = next((r for r in runs if r["candidate_hash"] == version_id), None)
    if earlier is None and row["state"] != "frozen":
        raise HoldoutError(f"only a frozen version is evaluated on the holdout ({version_id} is {row['state']})")
    if earlier is not None:
        exp = store.get_experiment(earlier["experiment_id"])
        prior = earlier["prior_runs_on_split"]
    else:
        cap = split_viewing_cap(store, version.split_id)
        recent = [r for r in runs if r["candidate_hash"].startswith("pv-")
                  and now() - r["run_at"] < VERSION_BATCH_SECONDS]
        refusal = (f"holdout of {version.split_id} already viewed {len(runs)} time(s); the limit is {cap}"
                   if len(runs) >= cap else
                   f"portfolio version {recent[-1]['candidate_hash']} viewed this split's holdout less than 7 days "
                   "ago (A9)" if recent else None)
        if refusal and not override_reason:
            raise HoldoutError(refusal)
        data = load_holdout_data(paths, version.split_id)
        exp = record_run(paths, store, version, "holdout", "holdout", data.bars, data.window)
        prior = store.record_holdout_run(version.split_id, version_id, exp["id"])
        if refusal:
            store.record_holdout_override(version.split_id, exp["id"], override_reason)
            note = f"{refusal}; overridden ({override_reason}); this split's holdout is contaminated"
        elif prior == 0:
            note = "first holdout viewing on this split"
        else:
            note = f"holdout already viewed {prior} time(s) on this split; result is no longer an untouched test"
        store.set_notes(exp["id"], note)
    m = exp["metrics"]
    passed = (m["sharpe_daily"] >= PORTFOLIO_GATE_3["min_sharpe_daily"]
              and m["total_return"] > PORTFOLIO_GATE_3["min_total_return"])
    evidence = {"holdout_experiment": exp["id"], "prior_viewings": prior, "rule": PORTFOLIO_GATE_3,
                "sharpe_daily": m["sharpe_daily"], "total_return": m["total_return"],
                "max_drawdown": m["max_drawdown"], "max_drawdown_no_ladder": m["max_drawdown_no_ladder"],
                "benchmarks": exp.get("benchmarks")}
    if vs.get(version_id)["state"] == "frozen":
        vs.transition(version_id, "final_evaluated" if passed else "failed_final", "evaluator", evidence)
        repeat = False
    else:
        repeat = earlier is not None
    return {"version_id": version_id, "state": vs.get(version_id)["state"], "passed": passed, "repeat": repeat,
            **evidence}
