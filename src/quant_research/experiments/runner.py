"""ExperimentRequest -> ExperimentResult, with every attempt recorded (including failures).

Flow: Gatekeeper (pre-run rules) -> compute (targets, lookahead check, backtest, metrics, statistics,
attribution, diagnosis) -> persist -> deflate Sharpe with the split's cumulative trial count.
"""

from __future__ import annotations

import json
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field, ValidationError

from quant_research import ENGINE_VERSION
from quant_research.backtest.engine import CostConfig, EngineConfig, run_backtest
from quant_research.data.bars import to_epoch
from quant_research.data.snapshot import load_manifest
from quant_research.evaluation.diagnosis import attribute, diagnose, significance_label
from quant_research.evaluation.metrics import compute_metrics
from quant_research.evaluation.statistics import deflate, sharpe_statistics
from quant_research.experiments.gatekeeper import GateContext, GateDecision, check, gaps_in_range
from quant_research.experiments.store import ExperimentStore, new_id
from quant_research.paths import Paths
from quant_research.portfolio.policy import PortfolioPolicy
from quant_research.strategy.compute import TargetResult, check_positions, compute_targets, strategy_weights
from quant_research.strategy.sandbox import SandboxConfig, SandboxError
from quant_research.strategy.spec import StrategyError, StrategySpec
from quant_research.timeutil import granularity_seconds, periods_per_year
from quant_research.validation.splits import SegmentData, load_research_segment, load_split, slice_window


class RejectedError(Exception):
    """The candidate violates a research rule (lookahead, gaps, invalid parameters/targets)."""


class ExperimentRequest(BaseModel):
    strategy_ref: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    dataset_id: str
    split_id: str
    segment: Literal["dev", "validation"] = "dev"
    engine_config: EngineConfig = Field(default_factory=EngineConfig)
    policy: PortfolioPolicy = Field(default_factory=PortfolioPolicy)
    allow_gaps: bool = False
    lookahead_checks: int = 12

    @property
    def spec(self) -> StrategySpec:
        return StrategySpec(strategy_ref=self.strategy_ref, parameters=self.parameters)


class ExperimentResult(BaseModel):
    experiment_id: str
    status: str  # completed | duplicate | rejected | failed | blocked | aborted
    metrics: dict[str, Any] | None = None
    benchmarks: dict[str, Any] | None = None
    checks: dict[str, Any] | None = None
    statistics: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None
    failure_reason: str | None = None
    result_dir: str | None = None
    duplicate_of: str | None = None

    @property
    def has_result(self) -> bool:
        return self.status in ("completed", "duplicate")

    @property
    def labels(self) -> list[str]:
        return [x["label"] for x in (self.diagnosis or {}).get("labels", [])]


def compute_experiment(
    paths: Paths,
    request: ExperimentRequest,
    sandbox: SandboxConfig | None = None,
    data: SegmentData | None = None,
) -> dict[str, Any]:
    """Pure computation (no DB writes) so it can run in worker processes."""
    try:
        spec = request.spec.normalized()
    except (ValidationError, StrategyError, KeyError) as exc:
        raise RejectedError(f"invalid strategy/parameters: {exc}") from exc
    split = load_split(paths, request.split_id)
    if split.dataset_id != request.dataset_id:
        raise RejectedError(f"split {split.split_id} belongs to {split.dataset_id}, not {request.dataset_id}")
    manifest = load_manifest(paths, request.dataset_id)
    data = data or load_research_segment(paths, request.split_id, request.segment)
    bars = data.bars
    step = granularity_seconds(manifest["granularity"])
    opens = to_epoch(bars["bar_open_time"])
    gaps = gaps_in_range(manifest, int(opens[0]), data.window[1]) if manifest["has_unfilled_gaps"] else []
    if gaps and not request.allow_gaps:
        raise RejectedError(f"{len(gaps)} unfilled data gap(s) inside the used history; "
                            "re-freeze with an explicit fill policy or pass allow_gaps")
    mask = slice_window(bars, data.window)
    positions = check_positions(mask, request.lookahead_checks)
    try:
        tr = compute_targets(spec, bars, paths, sandbox, positions)
    except StrategyError as exc:
        raise RejectedError(str(exc)) from exc
    if not tr.lookahead_ok:
        raise RejectedError(f"lookahead detected: targets at bar indices {tr.lookahead_violations[:5]} "
                            "change when later bars are removed")
    ppy = periods_per_year(manifest["granularity"])
    try:
        weights, policy = strategy_weights(tr, request.policy, bars, step, ppy)
    except StrategyError as exc:
        raise RejectedError(str(exc)) from exc
    cfg = policy.engine_config(request.engine_config)
    bt = run_backtest(bars, weights, cfg, data.window, step)
    metrics = compute_metrics(bt, cfg.initial_cash, ppy)
    keys = ("total_return", "cagr", "sharpe", "max_drawdown")
    bh_metrics = compute_metrics(run_backtest(bars, np.ones(len(bars)), cfg, data.window, step), cfg.initial_cash, ppy)
    # Same policy, schedule and costs applied to a constant proposal at the candidate's average proposal.
    mean_proposal = float(tr.targets[mask].mean())
    matched_w, _ = strategy_weights(TargetResult(np.full(len(bars), mean_proposal), contract=tr.contract),
                                    request.policy, bars, step, ppy)
    matched = compute_metrics(run_backtest(bars, matched_w, cfg, data.window, step), cfg.initial_cash, ppy)
    benchmarks = {
        "buy_and_hold": {k: bh_metrics[k] for k in keys},
        "cash": {"total_return": 0.0, "cagr": 0.0, "sharpe": 0.0, "max_drawdown": 0.0},
        "policy_matched": {**{k: matched[k] for k in keys}, "proposal": mean_proposal},
    }
    checks = {"lookahead_positions_checked": len(tr.lookahead_checked), "lookahead_ok": True,
              "gaps_in_history": len(gaps), "gap_crossings": bt.gap_crossings,
              "open_position_at_end": bt.meta["open_trip"],
              "filled_bars_in_window": int(bars.loc[mask, "filled"].sum()),
              "strategy_contract": tr.contract, "policy_applied": policy.label(),
              "policy_applied_id": policy.policy_id()}

    equity = bt.equity["equity"].to_numpy(float)
    returns = np.diff(np.concatenate([[cfg.initial_cash], equity])) / np.concatenate([[cfg.initial_cash],
                                                                                      equity[:-1]])
    gross_cfg = cfg.model_copy(update={"costs": CostConfig(fee_rate=0, slippage_bps=0, source="zero (attribution)")})
    gross = run_backtest(bars, weights, gross_cfg, data.window, step)
    attribution = attribute(bt, gross, bars, mask, cfg.initial_cash, ppy, step)
    return {
        "spec": spec.model_dump(),
        "strategy_hash": spec.strategy_hash(),
        "candidate_hash": spec.candidate_hash(),
        "data_hash": manifest["data_hash"],
        "window": data.window,
        "metrics": metrics,
        "benchmarks": benchmarks,
        "checks": checks,
        "statistics": sharpe_statistics(returns, ppy),
        "diagnosis": {"attribution": attribution, "labels": diagnose(metrics, attribution, benchmarks)},
        "periods_per_year": ppy,
        "equity": bt.equity,
        "fills": bt.fills,
        "trips": bt.trips,
        "targets": weights[mask],
    }


def _base_fields(request: ExperimentRequest, kind: str, segment: str, session_id: str | None,
                 parent_id: str | None, llm_suggestion: Any) -> dict[str, Any]:
    return {
        "kind": kind, "session_id": session_id, "parent_id": parent_id,
        "strategy_ref": request.strategy_ref, "parameters": request.parameters,
        "dataset_id": request.dataset_id, "split_id": request.split_id, "segment": segment,
        "engine_config_id": request.engine_config.config_id(),
        "engine_config": request.engine_config.model_dump(), "engine_version": ENGINE_VERSION,
        "policy_id": request.policy.policy_id(), "policy": request.policy.model_dump(),
        "llm_suggestion": llm_suggestion,
    }


def _deflated(store: ExperimentStore, split_id: str, stats: dict[str, Any], diagnosis: dict[str, Any],
              ppy: float, kind: str = "backtest") -> tuple[dict[str, Any], dict[str, Any]]:
    """Attach the DSR for the trials made so far on this split (the trial count only grows).

    Research selection bias is deflated during research; a holdout test of a frozen candidate is deflated only
    by the number of holdout viewings on the split.
    """
    if kind == "holdout":
        stats = deflate(stats, len(store.holdout_runs(split_id)) + 1, [], ppy)
    else:
        stats = deflate(stats, store.trial_count(split_id=split_id), store.research_sharpes(split_id), ppy)
    labels = [x for x in diagnosis["labels"] if x["label"] != "not_significant"]
    sig = significance_label(stats)
    return stats, {**diagnosis, "labels": labels + ([sig] if sig else [])}


def _persist(paths: Paths, store: ExperimentStore, exp_id: str, computed: dict[str, Any],
             split_id: str, kind: str = "backtest") -> ExperimentResult:
    out = paths.results / exp_id
    out.mkdir(parents=True, exist_ok=True)
    computed["equity"].to_parquet(out / "equity.parquet", index=False)
    computed["fills"].to_parquet(out / "fills.parquet", index=False)
    computed["trips"].to_parquet(out / "trips.parquet", index=False)
    store.finish_experiment(
        exp_id, "completed",
        parameters=computed["spec"]["parameters"],
        strategy_hash=computed["strategy_hash"], candidate_hash=computed["candidate_hash"],
        data_hash=computed["data_hash"], window_start=computed["window"][0], window_end=computed["window"][1],
        metrics=computed["metrics"], benchmarks=computed["benchmarks"], checks=computed["checks"],
        result_dir=str(out),
    )
    # Deflate after this trial is counted.
    stats, diagnosis = _deflated(store, split_id, computed["statistics"], computed["diagnosis"],
                                 computed["periods_per_year"], kind)
    store.finish_experiment(exp_id, "completed", statistics=stats, diagnosis=diagnosis)
    summary = {k: computed[k] for k in ("spec", "strategy_hash", "candidate_hash", "data_hash", "window",
                                        "metrics", "benchmarks", "checks")} | {"statistics": stats,
                                                                               "diagnosis": diagnosis}
    (out / "result.json").write_text(json.dumps(summary, indent=2, default=str))
    return ExperimentResult(experiment_id=exp_id, status="completed", metrics=computed["metrics"],
                            benchmarks=computed["benchmarks"], checks=computed["checks"], statistics=stats,
                            diagnosis=diagnosis, result_dir=str(out))


def _fail(store: ExperimentStore, exp_id: str, exc: BaseException) -> ExperimentResult:
    if isinstance(exc, RejectedError):
        status, reason = "rejected", str(exc)
    elif isinstance(exc, SandboxError):
        status, reason = "failed", f"candidate error: {exc}"
    else:
        status, reason = "failed", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}"
    store.finish_experiment(exp_id, status, failure_reason=reason)
    return ExperimentResult(experiment_id=exp_id, status=status, failure_reason=reason)


def _record_gate(paths: Paths, store: ExperimentStore, request: ExperimentRequest, decision: GateDecision,
                 base: dict[str, Any]) -> ExperimentResult:
    """Record a non-ok gate decision as an experiment row so every attempt is visible."""
    exp_id = new_id("exp")
    reason = "gate: " + "; ".join(decision.reasons)
    if decision.status != "duplicate":
        store.start_experiment(id=exp_id, **base)
        store.finish_experiment(exp_id, decision.status, failure_reason=reason)
        return ExperimentResult(experiment_id=exp_id, status=decision.status, failure_reason=reason)
    prior = store.get_experiment(decision.duplicate_of)
    store.start_experiment(id=exp_id, **{**base, "parent_id": base["parent_id"] or prior["id"]})
    copied = {k: prior[k] for k in ("strategy_hash", "candidate_hash", "data_hash", "window_start", "window_end",
                                    "metrics", "benchmarks", "checks", "result_dir")}
    store.finish_experiment(exp_id, "duplicate", failure_reason=reason, **copied)
    stats, diagnosis = prior["statistics"], prior["diagnosis"]
    if stats and diagnosis:
        ppy = periods_per_year(load_manifest(paths, request.dataset_id)["granularity"])
        stats, diagnosis = _deflated(store, request.split_id, stats, diagnosis, ppy, base["kind"])
        store.finish_experiment(exp_id, "duplicate", statistics=stats, diagnosis=diagnosis)
    return ExperimentResult(experiment_id=exp_id, status="duplicate", metrics=prior["metrics"],
                            benchmarks=prior["benchmarks"], checks=prior["checks"], statistics=stats,
                            diagnosis=diagnosis, result_dir=prior["result_dir"], duplicate_of=prior["id"],
                            failure_reason=reason)


def run_experiment(
    paths: Paths,
    store: ExperimentStore,
    request: ExperimentRequest,
    *,
    kind: str = "backtest",
    session_id: str | None = None,
    parent_id: str | None = None,
    llm_suggestion: Any = None,
    sandbox: SandboxConfig | None = None,
    data: SegmentData | None = None,
    gate: GateContext | None = None,
) -> ExperimentResult:
    segment = data.segment if data is not None else request.segment
    base = _base_fields(request, kind, segment, session_id, parent_id, llm_suggestion)
    gate = gate or GateContext(session_id=session_id)
    decision = check(paths, store, request, gate, sandbox, data.window if data is not None else None,
                     "holdout" if segment == "holdout" else segment.split(":")[0])
    if not decision.allowed:
        return _record_gate(paths, store, request, decision, base)
    exp_id = store.start_experiment(**base)
    try:
        computed = compute_experiment(paths, request, sandbox, data)
    except KeyboardInterrupt:
        store.finish_experiment(exp_id, "aborted", failure_reason="interrupted by user")
        raise
    except Exception as exc:
        return _fail(store, exp_id, exc)
    return _persist(paths, store, exp_id, computed, request.split_id, kind)


def _worker(args: tuple[Paths, ExperimentRequest, SandboxConfig | None]) -> dict[str, Any]:
    paths, request, sandbox = args
    return compute_experiment(paths, request, sandbox)


def run_many(
    paths: Paths,
    store: ExperimentStore,
    requests: list[ExperimentRequest],
    *,
    workers: int = 1,
    kind: str = "backtest",
    session_id: str | None = None,
    parent_id: str | None = None,
    sandbox: SandboxConfig | None = None,
    gate: GateContext | None = None,
) -> list[ExperimentResult]:
    """Run independent experiments, optionally in a process pool; only this process writes the DB."""
    gate = gate or GateContext(session_id=session_id)
    results: dict[int, ExperimentResult] = {}
    pending: dict[int, tuple[str, ExperimentRequest]] = {}
    in_batch: set[tuple[str, str]] = set()
    for i, req in enumerate(requests):
        base = _base_fields(req, kind, req.segment, session_id, parent_id, None)
        decision = check(paths, store, req, gate, sandbox)
        if decision.allowed and (decision.spec.candidate_hash(), req.segment) in in_batch:
            decision = GateDecision(status="blocked", reasons=["identical request earlier in the same batch"])
        if not decision.allowed:
            results[i] = _record_gate(paths, store, req, decision, base)
        else:
            in_batch.add((decision.spec.candidate_hash(), req.segment))
            pending[i] = (store.start_experiment(**base), req)

    def _abort_rest() -> None:
        for j, (exp_id, _) in pending.items():
            if j not in results:
                store.finish_experiment(exp_id, "aborted", failure_reason="interrupted by user")

    if workers <= 1:
        for i, (exp_id, req) in pending.items():
            try:
                results[i] = _persist(paths, store, exp_id, compute_experiment(paths, req, sandbox), req.split_id,
                                      kind)
            except KeyboardInterrupt:
                _abort_rest()
                raise
            except Exception as exc:
                results[i] = _fail(store, exp_id, exc)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_worker, (paths, req, sandbox)): i for i, (_, req) in pending.items()}
            try:
                for fut in as_completed(futures):
                    i = futures[fut]
                    exp_id, req = pending[i]
                    try:
                        results[i] = _persist(paths, store, exp_id, fut.result(), req.split_id, kind)
                    except Exception as exc:
                        results[i] = _fail(store, exp_id, exc)
            except KeyboardInterrupt:
                _abort_rest()
                raise
    return [results[i] for i in range(len(requests))]
