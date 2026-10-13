"""Mandate acceptance evaluation, shared by the controller and the manual workbench.

`single_validation`: the candidate on the validation segment plus the mandate's sensitivity cases.
`walk_forward_oos`: the plan's parameter grid re-selected on rolling windows over dev+validation; the procedure
is evaluated on the concatenated out-of-sample windows, and sensitivity cases rerun the whole procedure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from quant_research.experiments.gatekeeper import GateContext
from quant_research.experiments.runner import ExperimentRequest, run_experiment
from quant_research.experiments.store import ExperimentStore
from quant_research.paths import Paths
from quant_research.research.mandate import AcceptanceCriteria, Mandate, WalkForwardOOS, metric_value
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.validation.walkforward import walk_forward

SENSITIVITY_MESSAGES = {"costs_x2": "not profitable with doubled costs",
                        "delay_1_adverse": "not profitable with a one-bar delay and adverse fills"}


@dataclass
class Evaluation:
    verdict: str  # accepted | rejected | <experiment status when evaluation could not complete>
    reasons: list[str]
    primary: float | None = None
    metrics: dict[str, Any] | None = None
    statistics: dict[str, Any] | None = None
    benchmarks: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None
    experiments: dict[str, str] = field(default_factory=dict)  # role -> experiment id

    def evidence(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reasons": self.reasons, "primary": self.primary,
                "experiments": self.experiments, "metrics": self.metrics,
                "dsr": (self.statistics or {}).get("dsr")}


def acceptance_reasons(primary_metric: str, acc: AcceptanceCriteria, metrics: dict[str, Any],
                       statistics: dict[str, Any] | None, benchmarks: dict[str, Any] | None,
                       sensitivity: dict[str, dict[str, Any] | None]) -> list[str]:
    s = statistics or {}
    reasons = []
    value = metric_value(metrics, primary_metric)
    if value < acc.min_primary:
        reasons.append(f"{primary_metric} {value:.2f} < {acc.min_primary}")
    if acc.min_dsr is not None and s.get("dsr", 0.0) < acc.min_dsr:
        reasons.append(f"deflated sharpe {s.get('dsr', 0.0):.2f} < {acc.min_dsr} ({s.get('n_trials')} trials on split)")
    if acc.require_bootstrap_lower_positive and s.get("sharpe_ci_05", -1.0) <= 0:
        reasons.append(f"bootstrap 5th-percentile sharpe {s.get('sharpe_ci_05', float('nan')):.2f} <= 0")
    if metrics.get("n_round_trips", 0) < acc.min_round_trips:
        reasons.append(f"{metrics.get('n_round_trips', 0)} round trips < {acc.min_round_trips}")
    if metrics.get("max_drawdown", 0.0) < acc.max_drawdown:
        reasons.append(f"drawdown {metrics['max_drawdown']:.1%} worse than {acc.max_drawdown:.0%}")
    matched = (benchmarks or {}).get("policy_matched")
    if acc.require_beats_policy_matched and matched and metrics["sharpe"] <= matched["sharpe"]:
        reasons.append(f"sharpe {metrics['sharpe']:.2f} does not beat policy-matched exposure {matched['sharpe']:.2f}")
    for case, m in sensitivity.items():
        if m is None or m.get("total_return", 0.0) <= 0:
            reasons.append(SENSITIVITY_MESSAGES[case])
    return reasons


def _cases(acc: AcceptanceCriteria) -> list[str]:
    return [c for c, on in (("costs_x2", acc.require_costs_x2), ("delay_1_adverse", acc.require_delay)) if on]


def evaluate_candidate(paths: Paths, store: ExperimentStore, mandate: Mandate, request: ExperimentRequest, *,
                       gate: GateContext, sandbox: SandboxConfig | None = None, session_id: str | None = None,
                       parent_id: str | None = None, grid: dict[str, list[Any]] | None = None,
                       llm_suggestion: Any = None, record: dict[str, Any] | None = None) -> Evaluation:
    acc, design = mandate.acceptance, mandate.evaluation_design
    base = request.model_copy(update={"engine_config": mandate.engine_config, "policy": mandate.policy,
                                      "dataset_id": mandate.dataset_id, "split_id": mandate.split_id})
    kw = {"session_id": session_id, "sandbox": sandbox}
    if isinstance(design, WalkForwardOOS):
        grid = grid or {k: [v] for k, v in base.parameters.items()}
        wf_kw = {**kw, "gate": gate, "metric": mandate.primary_metric, "through": "validation"}
        main = walk_forward(paths, store, base, grid, design.train_bars, design.test_bars, parent_id=parent_id,
                            record=record, **wf_kw)
        ev = Evaluation(verdict="", reasons=[], experiments={"walk_forward": main["parent_experiment"]})
        if "metrics" not in main:
            ev.verdict, ev.reasons = "failed", ["walk-forward produced no completed out-of-sample fold"]
            return ev
        sens: dict[str, dict | None] = {}
        for case in _cases(acc):
            res = walk_forward(paths, store, base.model_copy(update={"engine_config": mandate.engine_config
                                                                     .sensitivity(case)}),
                               grid, design.train_bars, design.test_bars, kind="sensitivity",
                               parent_id=main["parent_experiment"], **wf_kw)
            sens[case] = res.get("metrics")
            ev.experiments[case] = res["parent_experiment"]
        ev.metrics, ev.statistics = main["metrics"], main["statistics"]
    else:
        val = run_experiment(paths, store, base.model_copy(update={"segment": "validation"}), parent_id=parent_id,
                             llm_suggestion=llm_suggestion, gate=gate, record=record, **kw)
        ev = Evaluation(verdict="", reasons=[], experiments={"validation": val.experiment_id})
        if not val.has_result:
            ev.verdict, ev.reasons = val.status, [val.failure_reason or val.status]
            return ev
        sens = {}
        for case in _cases(acc):
            res = run_experiment(paths, store, base.model_copy(update={
                "segment": "validation", "engine_config": mandate.engine_config.sensitivity(case)}),
                kind="sensitivity", parent_id=val.experiment_id, gate=gate, **kw)
            sens[case] = res.metrics if res.has_result else None
            ev.experiments[case] = res.experiment_id
        ev.metrics, ev.statistics, ev.benchmarks, ev.diagnosis = (val.metrics, val.statistics, val.benchmarks,
                                                                  val.diagnosis)
    ev.primary = metric_value(ev.metrics, mandate.primary_metric)
    ev.reasons = acceptance_reasons(mandate.primary_metric, acc, ev.metrics, ev.statistics, ev.benchmarks, sens)
    ev.verdict = "accepted" if not ev.reasons else "rejected"
    return ev
