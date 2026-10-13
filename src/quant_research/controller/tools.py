"""Fixed tools the coding agent may call during a round. They enforce session permissions and budget."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quant_research.controller.budget import Usage
from quant_research.controller.controller import SessionConfig, SessionError, SessionState
from quant_research.experiments.gatekeeper import GateContext
from quant_research.experiments.runner import ExperimentRequest, run_experiment
from quant_research.experiments.store import ExperimentStore
from quant_research.paths import Paths
from quant_research.strategy.plugin import register_plugin
from quant_research.strategy.spec import load_spec_file

ACTIVE = {SessionState.RUNNING, SessionState.WAITING_FOR_AGENT}


def _session(store: ExperimentStore, sid: str) -> tuple[dict[str, Any], SessionConfig]:
    sess = store.get_session(sid)
    if sess is None:
        raise SessionError(f"unknown session {sid}")
    if SessionState(sess["state"]) not in ACTIVE:
        raise SessionError(f"session {sid} is {sess['state']}; tools are disabled")
    return sess, SessionConfig.model_validate(sess["config"])


def tool_backtest(paths: Paths, store: ExperimentStore, sid: str, spec_path: Path, segment: str) -> dict[str, Any]:
    """Exploratory backtest for the agent. All rules are enforced by the Gatekeeper; refusals raise."""
    if segment != "dev":
        raise PermissionError("agent tools may only use the dev segment; submit candidates and the controller "
                              "evaluates them on validation")
    sess, cfg = _session(store, sid)
    gate = GateContext(caller="agent_tool", session_id=sid, max_session_experiments=cfg.budget.max_experiments,
                       max_trials_per_strategy=cfg.budget.max_trials_per_strategy,
                       allow_weak_isolation=cfg.allow_weak_isolation, stop_file=Path(sess["workspace"]) / "STOP")
    if store.session_usage_count(sid) >= cfg.budget.max_experiments:
        raise SessionError("session experiment budget exhausted")
    spec = load_spec_file(spec_path, register_plugin=lambda code: register_plugin(paths, code))
    req = ExperimentRequest(strategy_ref=spec.strategy_ref, parameters=spec.parameters, dataset_id=cfg.dataset_id,
                            split_id=cfg.split_id, segment=segment, engine_config=cfg.engine_config,
                            policy=cfg.policy)
    res = run_experiment(paths, store, req, session_id=sid, sandbox=cfg.sandbox, gate=gate,
                         llm_suggestion={"source": "agent tool call", "spec_file": str(spec_path)})
    if res.status == "blocked":
        raise SessionError(res.failure_reason)
    return res.model_dump(exclude={"result_dir"})


def tool_history(store: ExperimentStore, sid: str) -> dict[str, Any]:
    sess = store.get_session(sid)
    if sess is None:
        raise SessionError(f"unknown session {sid}")
    rows = store.list_experiments(limit=200, session_id=sid)
    return {
        "usage": Usage.model_validate(sess["usage"]).model_dump(),
        "experiments": [{k: r[k] for k in ("id", "status", "strategy_ref", "parameters", "segment",
                                           "failure_reason")}
                        | {"sharpe": (r["metrics"] or {}).get("sharpe"),
                           "total_return": (r["metrics"] or {}).get("total_return"),
                           "dsr": (r["statistics"] or {}).get("dsr"),
                           "labels": [d["label"] for d in (r["diagnosis"] or {}).get("labels", [])]}
                        for r in rows],
    }
