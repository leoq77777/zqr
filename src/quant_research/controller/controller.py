"""Research Controller: budget, permissions, retries, stop/resume around an external coding agent."""

from __future__ import annotations

import json
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from quant_research.backtest.engine import EngineConfig
from quant_research.controller.agent import AgentRoundResult, AgentRunner, CandidateSubmission, ResearchTask
from quant_research.controller.budget import Budget, ModelPricing, Usage, exhausted
from quant_research.data.snapshot import load_manifest
from quant_research.evaluation.diagnosis import SUGGESTIONS
from quant_research.experiments.gatekeeper import GateContext
from quant_research.experiments.runner import ExperimentRequest, run_experiment
from quant_research.experiments.store import ExperimentStore
from quant_research.paths import Paths
from quant_research.portfolio.policy import PortfolioPolicy
from quant_research.report.markdown import write_session_report
from quant_research.research.evaluate import evaluate_candidate
from quant_research.research.mandate import AcceptanceCriteria, Mandate
from quant_research.research.plans import ResearchPlan, artifact_name, plan_status
from quant_research.research.registry import candidate_identity, register_candidate, transition
from quant_research.strategy.plugin import register_plugin
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.strategy.spec import StrategySpec
from quant_research.validation.splits import agent_data_path, load_split

STOP_FILE = "STOP"


class SessionState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_FOR_AGENT = "waiting_for_agent"
    COMPLETED = "completed"
    STOPPED = "stopped"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"


TERMINAL = {SessionState.COMPLETED, SessionState.STOPPED, SessionState.BUDGET_EXHAUSTED, SessionState.FAILED}
TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.CREATED: {SessionState.RUNNING, SessionState.STOPPED},
    SessionState.RUNNING: {SessionState.WAITING_FOR_AGENT} | TERMINAL,
    SessionState.WAITING_FOR_AGENT: {SessionState.RUNNING, SessionState.STOPPED},
}


class SessionError(RuntimeError):
    pass


def _primary(record: dict[str, Any] | None) -> float | None:
    if not record:
        return None
    return record.get("validation_primary", record.get("validation_sharpe"))


class SessionConfig(BaseModel):
    """A session runs under exactly one immutable mandate; the rest is runtime configuration."""

    mandate: Mandate
    sandbox: SandboxConfig = Field(default_factory=lambda: SandboxConfig(isolation="docker"))
    allow_weak_isolation: bool = False
    pricing: ModelPricing | None = None
    max_agent_retries: int = 1

    @property
    def question(self) -> str:
        return self.mandate.question

    @property
    def dataset_id(self) -> str:
        return self.mandate.dataset_id

    @property
    def split_id(self) -> str:
        return self.mandate.split_id

    @property
    def budget(self) -> Budget:
        return self.mandate.budget

    @property
    def acceptance(self) -> AcceptanceCriteria:
        return self.mandate.acceptance

    @property
    def engine_config(self) -> EngineConfig:
        return self.mandate.engine_config

    @property
    def policy(self) -> PortfolioPolicy:
        return self.mandate.policy


class ResearchController:
    def __init__(self, paths: Paths, store: ExperimentStore, runner: AgentRunner):
        self.paths = paths
        self.store = store
        self.runner = runner

    # -- session lifecycle -----------------------------------------------------------------------
    def create_session(self, cfg: SessionConfig) -> str:
        split = load_split(self.paths, cfg.split_id)
        if split.dataset_id != cfg.dataset_id:
            raise SessionError("split does not belong to dataset")
        self.store.put_mandate(cfg.mandate.mandate_id(), cfg.mandate.model_dump(mode="json"))
        sid = self.store.create_session(question=cfg.question, dataset_id=cfg.dataset_id, split_id=cfg.split_id,
                                        state=SessionState.CREATED, budget=cfg.budget.model_dump(),
                                        usage=Usage().model_dump(), config=cfg.model_dump(), workspace="")
        ws = self.paths.sessions / sid
        ws.mkdir(parents=True)
        self.store.update_session(sid, workspace=str(ws))
        self._save_summary(sid, {"candidates": [], "agent_reports": [], "best": None})
        self.store.log_session_event(sid, "created", {"question": cfg.question})
        return sid

    def _transition(self, sid: str, new: SessionState, reason: str | None = None) -> None:
        cur = SessionState(self.store.get_session(sid)["state"])
        if new not in TRANSITIONS.get(cur, set()):
            raise SessionError(f"illegal transition {cur} -> {new}")
        self.store.update_session(sid, state=new, stop_reason=reason)
        self.store.log_session_event(sid, "state", {"from": cur, "to": new, "reason": reason})

    def request_stop(self, sid: str) -> None:
        """Takes effect at the next round boundary of a running controller, or immediately if idle."""
        sess = self.store.get_session(sid)
        (Path(sess["workspace"]) / STOP_FILE).write_text("stop requested\n")
        if SessionState(sess["state"]) in (SessionState.CREATED, SessionState.WAITING_FOR_AGENT):
            self._transition(sid, SessionState.STOPPED, "stopped by user")
            self._finish(sid)

    def _config(self, sid: str) -> SessionConfig:
        return SessionConfig.model_validate(self.store.get_session(sid)["config"])

    def _usage(self, sid: str) -> Usage:
        usage = Usage.model_validate(self.store.get_session(sid)["usage"])
        usage.experiments = self.store.session_usage_count(sid)
        return usage

    def _gate(self, sid: str, cfg: SessionConfig) -> GateContext:
        return GateContext(caller="controller", session_id=sid, max_session_experiments=cfg.budget.max_experiments,
                           max_trials_per_strategy=cfg.budget.max_trials_per_strategy,
                           allow_weak_isolation=cfg.allow_weak_isolation,
                           stop_file=self.paths.sessions / sid / STOP_FILE)

    def _summary_path(self, sid: str) -> Path:
        return self.paths.sessions / sid / "summary.json"

    def _load_summary(self, sid: str) -> dict[str, Any]:
        return json.loads(self._summary_path(sid).read_text())

    def _save_summary(self, sid: str, summary: dict[str, Any]) -> None:
        self._summary_path(sid).write_text(json.dumps(summary, indent=2, default=str))

    # -- main loop -------------------------------------------------------------------------------
    def run(self, sid: str) -> dict[str, Any]:
        sess = self.store.get_session(sid)
        if sess is None:
            raise SessionError(f"unknown session {sid}")
        state = SessionState(sess["state"])
        if state in TERMINAL:
            return self._load_summary(sid)
        if state == SessionState.RUNNING:
            # A previous controller process died mid-round; its in-flight experiments become aborted.
            self.store.log_session_event(sid, "recovered", {"aborted_experiments": self.store.mark_interrupted()})
        else:
            self._transition(sid, SessionState.RUNNING)
        cfg = self._config(sid)
        ws = Path(sess["workspace"])
        started = time.monotonic()
        usage = self._usage(sid)
        base_wall = usage.wall_seconds
        try:
            while True:
                usage = self._usage(sid)
                usage.wall_seconds = base_wall + time.monotonic() - started
                self.store.update_session(sid, usage=usage.model_dump())
                if (ws / STOP_FILE).exists():
                    self._transition(sid, SessionState.STOPPED, "stopped by user")
                    break
                reason = exhausted(cfg.budget, usage)
                if reason:
                    self._transition(sid, SessionState.BUDGET_EXHAUSTED, reason)
                    break
                if usage.rounds_without_improvement >= cfg.budget.patience_rounds:
                    self._transition(sid, SessionState.COMPLETED,
                                     f"no validation improvement in {cfg.budget.patience_rounds} rounds")
                    break
                result = self._run_agent(sid, cfg, usage)
                if result is None:
                    self._transition(sid, SessionState.WAITING_FOR_AGENT, "waiting for submission.json")
                    self.store.update_session(sid, usage=self._usage_with_wall(sid, base_wall, started))
                    return self._load_summary(sid)
                improved = self._process_round(sid, cfg, usage, result)
                usage = self._usage(sid)
                usage.rounds += 1
                usage.rounds_without_improvement = 0 if improved else usage.rounds_without_improvement + 1
                self.store.update_session(sid, usage=usage.model_dump())
                if result.done:
                    self._transition(sid, SessionState.COMPLETED, "agent concluded research")
                    break
        except KeyboardInterrupt:
            self._transition(sid, SessionState.STOPPED, "interrupted")
            raise
        except Exception as exc:
            self._transition(sid, SessionState.FAILED, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if SessionState(self.store.get_session(sid)["state"]) in TERMINAL:
                self.store.update_session(sid, usage=self._usage_with_wall(sid, base_wall, started))
                self._finish(sid)
        return self._load_summary(sid)

    def _usage_with_wall(self, sid: str, base_wall: float, started: float) -> dict[str, Any]:
        usage = self._usage(sid)
        usage.wall_seconds = base_wall + time.monotonic() - started
        return usage.model_dump()

    def _finish(self, sid: str) -> None:
        summary = self._load_summary(sid)
        path = write_session_report(self.paths, self.store, sid, summary)
        self.store.log_session_event(sid, "report", {"path": str(path)})

    def _task(self, sid: str, cfg: SessionConfig, usage: Usage) -> ResearchTask:
        summary = self._load_summary(sid)
        b = cfg.budget
        remaining = {"rounds": b.max_rounds - usage.rounds, "candidates": b.max_candidates - usage.candidates,
                     "experiments": b.max_experiments - usage.experiments}
        history = [{k: c.get(k) for k in ("round", "strategy_ref", "parameters", "hypothesis", "dev_sharpe",
                                          "validation_sharpe", "validation_return", "validation_dsr",
                                          "gross_return", "cost_drag", "labels", "verdict", "reasons")}
                   for c in summary["candidates"]]
        return ResearchTask(
            session_id=sid, round=usage.rounds + 1, question=cfg.question, dataset_id=cfg.dataset_id,
            split_id=cfg.split_id,
            research_data_path=str(agent_data_path(self.paths, cfg.split_id)),
            candidate_dir=str(self.paths.sessions / sid / f"round_{usage.rounds + 1:03d}"),
            remaining=remaining, history=history, feedback=self._feedback(summary["candidates"]),
            acceptance={"primary_metric": cfg.mandate.primary_metric,
                        "evaluation_design": cfg.mandate.evaluation_design.model_dump(),
                        **cfg.acceptance.model_dump()},
            quant_home=str(self.paths.root),
            trials_on_split=self.store.trial_count(split_id=cfg.split_id),
        )

    @staticmethod
    def _feedback(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Failure patterns across evaluated candidates, most frequent first, with what to change."""
        counts: dict[str, dict[str, Any]] = {}
        for c in candidates:
            for label in c.get("labels") or []:
                entry = counts.setdefault(label, {"label": label, "count": 0, "examples": [],
                                                  "suggestion": SUGGESTIONS.get(label, "")})
                entry["count"] += 1
                if len(entry["examples"]) < 3:
                    entry["examples"].append({"strategy_ref": c["strategy_ref"], "parameters": c["parameters"]})
        return sorted(counts.values(), key=lambda e: -e["count"])

    def _run_agent(self, sid: str, cfg: SessionConfig, usage: Usage) -> AgentRoundResult | None:
        task = self._task(sid, cfg, usage)
        self.store.log_session_event(sid, "round_started", {"round": task.round})
        last_error = None
        for attempt in range(cfg.max_agent_retries + 1):
            try:
                result = self.runner.run_round(task)
                break
            except Exception as exc:  # agent adapter failure: retry within limits
                last_error = f"{type(exc).__name__}: {exc}"
                self.store.log_session_event(sid, "agent_error", {"round": task.round, "attempt": attempt,
                                                                  "error": last_error})
        else:
            raise SessionError(f"agent failed after retries: {last_error}")
        if result is not None:
            self._account_tokens(sid, cfg, result)
        return result

    def _account_tokens(self, sid: str, cfg: SessionConfig, result: AgentRoundResult) -> None:
        usage = self._usage(sid)
        if result.usage is None:
            usage.model_cost_known = False
        else:
            u = result.usage
            usage.input_tokens += u.input_tokens
            usage.cache_read_tokens += u.cache_read_tokens
            usage.output_tokens += u.output_tokens
            if cfg.pricing:  # priced at round end; a round spanning a peak boundary is priced by its end time
                usage.model_cost_usd += cfg.pricing.cost(u.input_tokens, u.output_tokens, u.cache_read_tokens)
            else:
                usage.model_cost_known = False
        self.store.update_session(sid, usage=usage.model_dump())

    # -- candidate evaluation --------------------------------------------------------------------
    def _process_round(self, sid: str, cfg: SessionConfig, usage: Usage, result: AgentRoundResult) -> bool:
        summary = self._load_summary(sid)
        round_no = usage.rounds + 1
        if result.report:
            summary["agent_reports"].append({"round": round_no, "report": result.report})
        best_before = _primary(summary["best"])
        candidate_dir = self.paths.sessions / sid / f"round_{round_no:03d}"
        for sub in result.candidates:
            usage = self._usage(sid)
            if exhausted(cfg.budget, usage):
                summary["candidates"].append({"round": round_no, "strategy_ref": "?", "parameters": sub.parameters,
                                              "hypothesis": sub.hypothesis, "verdict": "skipped",
                                              "reasons": ["budget exhausted before evaluation"]})
                continue
            record = self._evaluate(sid, cfg, round_no, candidate_dir, sub, result)
            usage = self._usage(sid)
            usage.candidates += 1
            self.store.update_session(sid, usage=usage.model_dump())
            summary["candidates"].append(record)
            if record["verdict"] == "accepted" and (summary["best"] is None
                                                    or _primary(record) > _primary(summary["best"])):
                summary["best"] = record
        self._save_summary(sid, summary)
        best_after = _primary(summary["best"])
        return best_after is not None and (best_before is None or best_after > best_before)

    def _plan(self, sid: str, cfg: SessionConfig, sub: CandidateSubmission, artifact: str) -> tuple[str, bool]:
        """(plan_id, retrospective). A submission without a preregistered plan gets a retrospective plan record,
        disclosed as such, because the agent may already have seen dev results for it."""
        if sub.plan_id:
            rec = self.store.get_plan(sub.plan_id)
            if rec is None or rec["session_id"] != sid:
                raise PermissionError(f"plan {sub.plan_id} does not belong to this session")
            return sub.plan_id, bool(rec["retrospective"])
        grid = sub.parameter_grid or {k: [v] for k, v in sub.parameters.items()}
        plan = ResearchPlan(hypothesis=sub.hypothesis or "(no hypothesis stated)", mechanism="not stated",
                            falsification="not stated", artifact=artifact,
                            parameter_ranges={k: {"values": v} for k, v in grid.items()}, planned_experiments=1,
                            primary_metric=cfg.mandate.primary_metric)
        return self.store.put_plan(sid, cfg.mandate.mandate_id(), plan.model_dump(), retrospective=True), True

    def _evaluate(self, sid: str, cfg: SessionConfig, round_no: int, candidate_dir: Path,
                  sub: CandidateSubmission, result: AgentRoundResult) -> dict[str, Any]:
        record: dict[str, Any] = {"round": round_no, "parameters": sub.parameters, "hypothesis": sub.hypothesis,
                                  "reasons": []}
        try:
            if sub.template:
                strategy_ref, artifact = f"template:{sub.template}", sub.template
            elif sub.plugin:
                code = (candidate_dir / sub.plugin).resolve()
                if candidate_dir.resolve() not in code.parents:
                    raise PermissionError("plugin path escapes the candidate directory")
                strategy_ref, artifact = f"plugin:{register_plugin(self.paths, code)}", sub.plugin
            else:
                raise ValueError("candidate needs 'template' or 'plugin'")
            plan_id, retrospective = self._plan(sid, cfg, sub, artifact)
        except Exception as exc:
            record.update(strategy_ref=sub.template or sub.plugin or "?", verdict="rejected",
                          reasons=[f"{type(exc).__name__}: {exc}"])
            return record
        plan = ResearchPlan.model_validate(self.store.get_plan(plan_id)["body"])
        status, deviation = plan_status(plan, artifact_name(strategy_ref, artifact), sub.parameters,
                                        self.store.plan_usage(plan_id))
        record.update(strategy_ref=strategy_ref, plan_id=plan_id, plan_retrospective=retrospective,
                      plan_status=status, plan_deviation=deviation)
        ledger = {"plan_id": plan_id, "plan_status": status, "plan_deviation": deviation, "artifact": artifact}
        llm = {"hypothesis": sub.hypothesis, "submission": sub.model_dump(), "round": round_no}
        base = ExperimentRequest(strategy_ref=strategy_ref, parameters=sub.parameters, dataset_id=cfg.dataset_id,
                                 split_id=cfg.split_id, engine_config=cfg.engine_config, policy=cfg.policy)
        gate = self._gate(sid, cfg)
        dev = run_experiment(self.paths, self.store, base, session_id=sid, sandbox=cfg.sandbox, llm_suggestion=llm,
                             gate=gate, record=ledger)
        record["dev_experiment"] = dev.experiment_id
        if not dev.has_result:
            record.update(verdict=dev.status, reasons=[dev.failure_reason or dev.status])
            return record
        record["dev_sharpe"] = dev.metrics["sharpe"]
        record["labels"] = dev.labels
        spec = StrategySpec(strategy_ref=strategy_ref, parameters=sub.parameters).normalized()
        identity = candidate_identity(spec, load_manifest(self.paths, cfg.dataset_id)["data_hash"], cfg.split_id,
                                      cfg.engine_config.config_id(), cfg.policy.policy_id(), cfg.mandate.mandate_id())
        cid = register_candidate(self.store, identity, session_id=sid, plan_id=plan_id, actor="controller",
                                 evidence={"dev_experiment": dev.experiment_id})
        record["candidate_id"] = cid
        grid = sub.parameter_grid or (plan.parameter_grid() if plan.parameter_ranges else None)
        ev = evaluate_candidate(self.paths, self.store, cfg.mandate, base, gate=gate, sandbox=cfg.sandbox,
                                session_id=sid, parent_id=dev.experiment_id, grid=grid, llm_suggestion=llm,
                                record=ledger)
        record["evaluation_experiments"] = ev.experiments
        record["validation_experiment"] = ev.experiments.get("validation") or ev.experiments.get("walk_forward")
        open_ = self.store.get_candidate(cid)["state"] == "development"  # resubmissions keep their first verdict
        if ev.metrics is None:
            if open_:
                transition(self.store, cid, "rejected", "controller", ev.evidence(), "evaluation did not complete")
            record.update(verdict=ev.verdict, reasons=ev.reasons)
            return record
        labels = list(dev.labels) + [x["label"] for x in (ev.diagnosis or {}).get("labels", [])
                                     if x["label"] not in dev.labels]
        if dev.metrics["sharpe"] > 0 and ev.metrics["sharpe"] < 0.5 * dev.metrics["sharpe"]:
            labels.append("dev_validation_degradation")
        attribution = (ev.diagnosis or {}).get("attribution", {})
        record.update(validation_sharpe=ev.metrics["sharpe"], validation_primary=ev.primary,
                      validation_return=ev.metrics["total_return"],
                      validation_dsr=(ev.statistics or {}).get("dsr"),
                      gross_return=attribution.get("gross_total_return"), cost_drag=attribution.get("cost_drag"),
                      labels=labels, verdict=ev.verdict, reasons=ev.reasons)
        if open_:
            transition(self.store, cid, "validated" if ev.verdict == "accepted" else "rejected", "controller",
                       ev.evidence())
        return record
