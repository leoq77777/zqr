"""The desk's research loop: one isolated agent session at a time, around the clock.

Each session: pick a topic -> fresh session on the product's real-data mandate (agent tools see `dev` only) ->
agent container behind the M5 egress proxy and tool gateway -> freeze the workspace -> audit -> the declared
`final_artifact` is registered and evaluated deterministically on validation (DSR counts every trial on the
split) -> a validated candidate goes through automatic promotion. Every session leaves a record in
`desk/sessions/<run_id>/result.json` and a line in `desk/research_ledger.jsonl`.

Guards: a daily cost cap, a per-session wall clock and experiment budget (the mandate's), exponential backoff on
failures, and a pause with the reason shown in `desk status` when the environment (model, Docker) is at fault.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from quant_research.bench.adapter import MODEL_ENV, AgentRun
from quant_research.bench.audit import audit_session, recover_session
from quant_research.bench.egress import GATEWAY_PORT, PROXY_ALIAS, EgressNetwork, cleanup_run, model_endpoint, summarize
from quant_research.bench.runner import TRANSCRIPT_DIR, _pricing, freeze_submission
from quant_research.bench.tasks import CONTRACT_V02, benchmark_dir
from quant_research.controller.agent import ManualRunner, usage_from_transcripts
from quant_research.controller.controller import ResearchController, SessionConfig, SessionState
from quant_research.data.snapshot import load_manifest
from quant_research.desk import softstop
from quant_research.desk.config import DeskConfig, effective_network, load_check
from quant_research.desk.promotion import promote
from quant_research.desk.supervisor import CREDENTIAL_FILE, read_env_file
from quant_research.experiments.gatekeeper import GateContext
from quant_research.experiments.runner import ExperimentRequest
from quant_research.experiments.store import ExperimentStore
from quant_research.gateway import ToolGateway
from quant_research.gateway.mcp_server import GatewayHTTPServer
from quant_research.paths import Paths
from quant_research.research.evaluate import evaluate_candidate
from quant_research.research.mandate import Mandate
from quant_research.research.plans import ResearchPlan
from quant_research.research.registry import candidate_identity, register_candidate, transition
from quant_research.runtime import version
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.strategy.spec import StrategySpec

log = logging.getLogger(__name__)

TOPICS: list[tuple[str, str]] = [
    ("trend", "Trend following / time-series momentum: does a persistent-trend rule on hourly bars survive "
              "costs and a one-bar delay?"),
    ("mean_reversion", "Short-horizon mean reversion: do hourly overreactions revert enough to pay for costs "
                       "while staying long-or-cash?"),
    ("volatility_filter", "Volatility regime filter: does reducing or exiting exposure in high-volatility regimes "
                          "improve the risk-adjusted return of a simple long rule?"),
    ("multi_timeframe", "Multi-timeframe confirmation: does requiring agreement between a slow (multi-day) and a "
                        "fast (hourly) signal reduce whipsaw and improve net returns?"),
]
ENV_FAILURES = {"environment.model_unreachable", "environment.docker", "harness.error"}
PROMPT = ("You are the research desk's strategy researcher working inside /workspace. Read /workspace/ROLE.md "
          "(your role) and /workspace/TASK.md (the question, budget, tools and deliverables), then do the research "
          "using only the `quant` MCP tools for experiments. Write all deliverables under /workspace and validate "
          "research.json with the write_report tool before you finish.")


def sessions_dir(paths: Paths) -> Path:
    return paths.desk / "sessions"


def ledger_path(paths: Paths) -> Path:
    return paths.desk / "research_ledger.jsonl"


def state_path(paths: Paths) -> Path:
    return paths.desk / "research_state.json"


def _have_credentials() -> bool:
    """The research child is the only process allowed model credentials, so it may pick up a key entered after it
    started (`zqr desk setup --rekey`) without a restart."""
    names = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
    if not any(os.environ.get(n) for n in names):
        os.environ.update({k: v for k, v in read_env_file(CREDENTIAL_FILE).items() if k in MODEL_ENV and v})
    return any(os.environ.get(n) for n in names)


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def read_ledger(paths: Paths) -> list[dict[str, Any]]:
    p = ledger_path(paths)
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def spent_on(paths: Paths, day: str) -> float:
    return sum(r.get("cost_usd") or 0.0 for r in read_ledger(paths) if r.get("day") == day)


def load_state(paths: Paths) -> dict[str, Any]:
    p = state_path(paths)
    return json.loads(p.read_text()) if p.exists() else {"sessions_started": 0, "consecutive_failures": 0}


def _save_state(paths: Paths, state: dict[str, Any], **update: Any) -> dict[str, Any]:
    state.update(update, updated_at=time.time())
    tmp = state_path(paths).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(state_path(paths))
    return state


def candidates_summary(store: ExperimentStore, mandate_id: str, limit: int = 15) -> list[str]:
    rows = [c for c in store.list_candidates() if c["mandate_id"] == mandate_id]
    out = []
    for c in rows[-limit:]:
        ev = c["evidence"] or {}
        metrics = ev.get("metrics") or {}
        sharpe = metrics.get("sharpe")
        out.append(f"- `{c['id']}` {c['state']}: {c['strategy_ref'][:20]} params={json.dumps(c['parameters'])[:80]}"
                   + (f" validation sharpe {sharpe:.2f}" if isinstance(sharpe, (int, float)) else "")
                   + (f"; reasons: {'; '.join(ev.get('reasons') or [])[:160]}" if ev.get("reasons") else ""))
    return out


def task_brief(run_id: str, product: str, topic: tuple[str, str], mandate: Mandate, prior: list[str]) -> str:
    b, acc = mandate.budget, mandate.acceptance
    prior_txt = "\n".join(prior) if prior else "- (none yet)"
    return f"""# Desk research session

research_id: `{run_id}`  task_id: `desk-{topic[0]}`

## Question

{product}, hourly bars, long-or-cash spot. {topic[1]}

Find at most one strategy worth submitting, or conclude that the evidence does not support one. A null result is a
valid outcome; do not manufacture alpha.

## What happens to your submission

The controller re-runs your declared `final_artifact` on the **validation** segment (which you cannot see) and
accepts it only if all of these hold: {mandate.primary_metric} >= {acc.min_primary}; deflated Sharpe
{f">= {acc.min_dsr}" if acc.min_dsr is not None else "waived"} counting **every trial ever run on this split**
(each extra experiment you run lowers everyone's DSR); at least {acc.min_round_trips} round trips; max drawdown
not worse than {acc.max_drawdown:.0%}; {"still profitable with doubled costs; " if acc.require_costs_x2 else ""}
{"still profitable with a one-bar delay." if acc.require_delay else ""}
Accepted candidates may later be tested once on a sealed holdout and then paper traded.

## Earlier candidates on this mandate (avoid repeating them)

{prior_txt}

## Budget (enforced)

- Experiments: {b.max_experiments} on the `dev` segment. Wall clock: {int(b.max_wall_seconds or 0)} s.

## Tools (MCP server `quant`)

- `inspect_dataset` -> dataset identity, schema, allowed segments, costs and policy.
- `submit_plan(plan)` -> plan_id. Preregister hypothesis, mechanism, falsification, artifact, parameter_ranges,
  planned_experiments before running experiments.
- `submit_experiment(artifact, idempotency_key, parameters, plan_id, segment)` -> experiment record. `artifact` is
  a `.py` file under `/workspace/` implementing `/workspace/fixtures/strategy_contract.md`. Use a new idempotency
  key per intended experiment and the same key when retrying after a timeout.
- `get_experiment(experiment_id)`, `compare_experiments(experiment_ids)`, `history()`.
- `write_report("research.json")` validates your research.json.

There is no shell and no network besides these tools. Files you may write: anything under `/workspace/`.

## Deliverables

- `/workspace/research.json` following `/workspace/research.schema.json`; list every experiment id you ran.
- `/workspace/report.md`: what you did, what the evidence supports and does not, all attempts, next step.
- `final_artifact` in research.json: `{{"path", "sha256", "parameters", "experiment_id"}}` where the experiment is a
  completed `dev` run of exactly that file with exactly those parameters, or `null` if you recommend nothing.
Call `write_report` before finishing and fix any problem it reports. The workspace is frozen when you finish.
"""


class ResearchLoop:
    def __init__(self, paths: Paths, store: ExperimentStore, cfg: DeskConfig, adapter, *,
                 sandbox: SandboxConfig | None = None, allow_weak_isolation: bool = False,
                 isolate_network: bool = True, require_check: bool = True,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.paths, self.store, self.cfg, self.adapter = paths, store, cfg, adapter
        self.sandbox = sandbox or SandboxConfig(isolation="docker")
        self.allow_weak = allow_weak_isolation
        self.isolate_network, self.require_check = isolate_network, require_check
        self.clock, self.sleep = clock, sleep
        self.stop_requested = False
        sessions_dir(paths).mkdir(parents=True, exist_ok=True)
        self.state = load_state(paths)

    # -- scheduling ------------------------------------------------------------------------------
    def pick_topic(self, n: int) -> tuple[str, tuple[str, str]]:
        products = [p for p in self.cfg.products if p in self.cfg.mandates]
        return products[n % len(products)], TOPICS[(n // len(products)) % len(TOPICS)]

    def _wait(self, status: str, reason: str, seconds: float) -> dict[str, Any]:
        self.state = _save_state(self.paths, self.state, status=status, reason=reason,
                                 resume_at=self.clock() + seconds)
        return {"ran": False, "status": status, "reason": reason, "wait": seconds}

    def step(self) -> dict[str, Any]:
        """Run one session if allowed, otherwise say how long to wait and why."""
        now = self.clock()
        if not self.cfg.research_enabled:
            return self._wait("disabled", "research_enabled is false in desk/config.json", 600)
        if softstop.active(self.paths) is not None:
            return self._wait("paused", "soft stop: human review required (`zqr desk soft-stop clear --note`)", 600)
        if not [p for p in self.cfg.products if p in self.cfg.mandates]:
            return self._wait("blocked", "no desk mandates; run `zqr desk setup`", 600)
        if self.require_check and not load_check(self.paths).get("isolation_ok"):
            return self._wait("blocked", "isolation probe has not passed; run `zqr desk check`", 600)
        if self.require_check and not _have_credentials():
            return self._wait("blocked", f"no model credential; run `zqr secrets set` ({CREDENTIAL_FILE})", 60)
        resume_at = self.state.get("resume_at") or 0
        if self.state.get("status") in ("paused", "backoff") and now < resume_at:
            return {"ran": False, "status": self.state["status"], "reason": self.state.get("reason"),
                    "wait": resume_at - now}
        day = utc_day(now)
        spent = spent_on(self.paths, day)
        if spent >= self.cfg.daily_cost_cap_usd:
            tomorrow = datetime.fromtimestamp(now, tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            wait = (tomorrow + timedelta(days=1)).timestamp() - now + 1
            return self._wait("cost_cap", f"daily cost cap reached ({spent:.2f} >= {self.cfg.daily_cost_cap_usd} USD)",
                              wait)
        if sum(r.get("day") == day for r in read_ledger(self.paths)) >= self.cfg.max_sessions_per_day:
            return self._wait("cost_cap", f"max_sessions_per_day ({self.cfg.max_sessions_per_day}) reached", 3600)
        res = self.run_session()
        failure = res["failure_class"]
        n = self.state.get("consecutive_failures", 0) + 1 if failure != "none" else 0
        if failure == "desk.stopped":
            return {"ran": True, "status": "stopping", "result": res}
        if failure in ENV_FAILURES:
            delay = min(self.cfg.backoff_base_seconds * 2 ** (n - 1), self.cfg.backoff_max_seconds)
            self.state = _save_state(self.paths, self.state, status="paused", consecutive_failures=n,
                                     reason=f"{failure}: {'; '.join(res['failure_details'])[:300]}",
                                     resume_at=self.clock() + delay)
        elif failure != "none":
            delay = min(60 * 2 ** (n - 1), self.cfg.backoff_max_seconds)
            self.state = _save_state(self.paths, self.state, status="backoff", consecutive_failures=n,
                                     reason=failure, resume_at=self.clock() + delay)
        else:
            self.state = _save_state(self.paths, self.state, status="running", consecutive_failures=0, reason=None,
                                     resume_at=None)
        return {"ran": True, "status": self.state["status"], "result": res}

    def request_stop(self) -> None:
        """Signal-safe: stop after the current session, ending it now by removing its containers."""
        self.stop_requested = True
        current = self.state.get("current_run")
        if current:
            cleanup_run(current)

    def run(self, max_sessions: int | None = None) -> None:
        ran = 0
        while not self.stop_requested and (max_sessions is None or ran < max_sessions):
            out = self.step()
            if out["ran"]:
                ran += 1
                continue
            waited = 0.0
            while not self.stop_requested and waited < out["wait"]:
                self.sleep(min(5.0, out["wait"] - waited))
                waited += 5.0
        self.state = _save_state(self.paths, self.state, status="stopped", current_run=None)

    # -- one session -----------------------------------------------------------------------------
    def _workspace(self, ws: Path, brief: str) -> None:
        root = benchmark_dir()
        (ws / "fixtures").mkdir(exist_ok=True)
        shutil.copyfile(root / "role_prompt.md", ws / "ROLE.md")
        shutil.copyfile(root / "v0.2" / "research.schema.json", ws / "research.schema.json")
        shutil.copyfile(root / CONTRACT_V02, ws / "fixtures" / "strategy_contract.md")
        (ws / "TASK.md").write_text(brief)

    def run_session(self) -> dict[str, Any]:
        n = self.state.get("sessions_started", 0)
        product, topic = self.pick_topic(n)
        mandate = Mandate.model_validate(self.store.get_mandate(self.cfg.mandates[product])["body"])
        run_id = f"desk-{datetime.fromtimestamp(self.clock(), tz=UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        sess_cfg = SessionConfig(mandate=mandate, sandbox=self.sandbox, allow_weak_isolation=self.allow_weak)
        sid = ResearchController(self.paths, self.store, ManualRunner()).create_session(sess_cfg)
        ws = Path(self.store.get_session(sid)["workspace"])
        self._workspace(ws, task_brief(run_id, product, topic, mandate,
                                       candidates_summary(self.store, mandate.mandate_id())))
        run_dir = sessions_dir(self.paths) / run_id
        run_dir.mkdir(parents=True)
        meta = {"run_id": run_id, "session_id": sid, "product": product, "topic": topic[0],
                "mandate_id": mandate.mandate_id(), "started_at": self.clock()}
        (run_dir / "run.json").write_text(json.dumps(meta, indent=2))
        self.state = _save_state(self.paths, self.state, sessions_started=n + 1, current_run=run_id,
                                 status="running", reason=f"session {run_id} ({product}, {topic[0]})")
        allow = [model_endpoint()]
        run: AgentRun | None = None
        harness_error, egress_records = None, []
        net_cfg = effective_network(self.cfg)
        try:
            self.store.update_session(sid, state=SessionState.RUNNING)
            with GatewayHTTPServer(self.paths, self.store, sid, host=net_cfg.gateway_bind,
                                   extra_hosts=(f"{PROXY_ALIAS}:{GATEWAY_PORT}",), run_label=run_id) as srv:
                if self.isolate_network:
                    with EgressNetwork(run_dir, allow, srv.port, label=run_id, forward_host=net_cfg.forward_host,
                                       control_network=net_cfg.control_network) as net:
                        run = self.adapter.run(ws, srv.client_config(url=net.gateway_url()),
                                               self.cfg.session_wall_seconds, network=net, label=run_id,
                                               transcript_dir=run_dir / TRANSCRIPT_DIR)
                        egress_records = net.records()
                else:
                    run = self.adapter.run(ws, srv.client_config(), self.cfg.session_wall_seconds)
            freeze_submission(ws, run_dir / "submission")
            if run.timed_out:
                recover_session(self.store, sid, "wall clock exceeded")
            else:
                self.store.update_session(sid, state=SessionState.COMPLETED, stop_reason="desk session finished")
                recover_session(self.store, sid, "post-run consistency check")
        except Exception as exc:  # noqa: BLE001 - every failure still ends in a terminal session and a record
            harness_error = f"{type(exc).__name__}: {exc}"
            (run_dir / "harness_error.txt").write_text(traceback.format_exc())
            recover_session(self.store, sid, f"harness error: {harness_error[:200]}")
            cleanup_run(run_id)
        return self.finish(run_dir, meta, mandate, run=run, egress_records=egress_records,
                           harness_error=harness_error)

    def finish(self, run_dir: Path, meta: dict[str, Any], mandate: Mandate, *, run: AgentRun | None,
               egress_records: list[dict[str, Any]], harness_error: str | None, crash: bool = False) -> dict[str, Any]:
        sid = meta["session_id"]
        audit = audit_session(self.store, sid)
        egress = summarize(egress_records) if (self.isolate_network and egress_records) else None
        research_file = run_dir / "submission" / "research.json"
        research = None
        if research_file.is_file():
            try:
                research = json.loads(research_file.read_text())
            except ValueError:
                research = None
        u = run.usage if run else usage_from_transcripts(run_dir / TRANSCRIPT_DIR)
        cost, cost_source = self._cost(run, u)
        failure, details = self._classify(run, audit, egress, harness_error, research, crash)
        if self.stop_requested and failure not in ("none", "audit.failed"):
            failure, details = "desk.stopped", ["desk stop requested during the session", *details]
        events = self.store.session_events(sid)
        exps = self.store.list_experiments(limit=10_000, session_id=sid)
        model_host = {h for h, _ in [model_endpoint()]}
        violations = ([f"[tool_policy] {e['data'].get('tool')}: {e['data'].get('error')}"
                       for e in events if e["event"] == "tool_refused" and e["data"].get("policy")]
                      + [f"[gatekeeper] {e['id']}: {e['failure_reason']}" for e in exps if e["status"] == "blocked"]
                      + [f"[network] {d}" for d in (egress or {}).get("denied", [])
                         if d.split(" ", 1)[-1].split(":", 1)[0] not in model_host])
        outcome: dict[str, Any] = {"verdict": None}
        if failure == "none":
            outcome = self._evaluate(meta, mandate, research)
            if outcome.get("candidate_id") and outcome["verdict"] == "accepted":
                outcome["promotion"] = promote(self.paths, self.store, outcome["candidate_id"], self.sandbox)
        finished = self.clock()
        result = {
            "run_id": meta["run_id"], "session_id": sid, "product": meta["product"], "topic": meta["topic"],
            "mandate_id": meta["mandate_id"], "started_at": meta["started_at"], "finished_at": finished,
            "day": utc_day(meta["started_at"]), "wall_seconds": round(finished - meta["started_at"], 1),
            "status": "ok" if failure == "none" else "failed", "failure_class": failure,
            "failure_details": [str(d) for d in details if d][:5],
            "cost_usd": cost, "cost_source": cost_source, "usage": u.model_dump() if u else None,
            "zqr_version": version(), "harness": run.harness_version if run else None,
            "model": (", ".join(run.extra.get("models") or []) if run else None) or (u.model if u else None),
            "experiments": sum(e["status"] != "blocked" for e in exps
                               if e["kind"] not in ("sensitivity",) and e["segment"] == "dev"),
            "audit_ok": audit["ok"], "audit_problems": audit["problems"], "violations": violations,
            "egress": egress, **outcome,
        }
        (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        with open(ledger_path(self.paths), "a") as f:
            f.write(json.dumps({k: result.get(k) for k in (
                "run_id", "day", "product", "topic", "status", "failure_class", "cost_usd", "cost_source",
                "experiments", "audit_ok", "verdict", "candidate_id", "session_id", "zqr_version", "harness",
                "model")}
                | {"violations": len(violations),
                   "promotion": (result.get("promotion") or {}).get("stopped")}, default=str) + "\n")
        self.state = _save_state(self.paths, self.state, current_run=None, last_run_id=meta["run_id"])
        return result

    def _cost(self, run: AgentRun | None, u) -> tuple[float, str]:
        pricing = _pricing((run.extra.get("models") if run else None) or (u.model.split(",") if u and u.model
                                                                           else []), self.cfg.cny_per_usd)
        if u and pricing:
            return round(pricing.cost(u.input_tokens, u.output_tokens, u.cache_read_tokens, ts=time.time()), 6), \
                "priced_usage"
        if run is not None and run.cli_cost_usd is not None:
            return round(float(run.cli_cost_usd), 6), "cli_estimate"
        if u is None or (u.input_tokens + u.output_tokens) == 0:
            return 0.0, "no_tokens"
        return 0.0, "unpriced"

    @staticmethod
    def _classify(run: AgentRun | None, audit: dict, egress: dict | None, harness_error: str | None,
                  research: dict | None, crash: bool) -> tuple[str, list[str]]:
        if crash:
            return "harness.crash", [harness_error or "desk process died during the session"]
        if harness_error:
            return "harness.error", [harness_error]
        if not audit["ok"]:
            return "audit.failed", audit["problems"]
        no_model = run is None or run.usage is None or (run.usage.input_tokens + run.usage.output_tokens) == 0
        if no_model and egress is not None and egress["model_tunnels"] == 0:
            return "environment.model_unreachable", egress["denied"][:3] + egress["upstream_errors"][:3]
        if run is not None and run.timed_out:
            return "agent.timeout", ["wall clock exceeded"]
        if no_model:
            return "environment.docker", [(run.stderr_tail if run else "")[-400:]]
        if research is None:
            return "agent.no_deliverable", [f"exit {run.exit_code}; research.json missing or invalid",
                                            run.result_text[-200:]]
        return "none", []

    def _evaluate(self, meta: dict[str, Any], mandate: Mandate, research: dict[str, Any]) -> dict[str, Any]:
        sid = meta["session_id"]
        declared = research.get("final_artifact")
        if declared is None:
            return {"verdict": "no_candidate", "reasons": ["final_artifact is null"]}
        submission = sessions_dir(self.paths) / meta["run_id"] / "submission"
        problems = ToolGateway(self.paths, self.store, sid, workspace=submission).final_artifact_problems(declared)
        if problems:
            return {"verdict": "artifact_mismatch", "reasons": problems}
        rec = self.store.get_experiment(declared["experiment_id"])
        params = declared["parameters"] or {}
        plan_id = rec.get("plan_id")
        if not plan_id:
            hyp = research.get("hypothesis") if isinstance(research.get("hypothesis"), str) else ""
            plan = ResearchPlan(hypothesis=hyp if len(hyp) >= 10 else "(no hypothesis stated)",
                                mechanism="not stated", falsification="not stated", artifact=str(declared["path"]),
                                parameter_ranges={k: {"values": [v]} for k, v in params.items()},
                                planned_experiments=1, primary_metric=mandate.primary_metric)
            plan_id = self.store.put_plan(sid, mandate.mandate_id(), plan.model_dump(), retrospective=True)
        spec = StrategySpec(strategy_ref=rec["strategy_ref"], parameters=params).normalized()
        identity = candidate_identity(spec, load_manifest(self.paths, mandate.dataset_id)["data_hash"],
                                      mandate.split_id, mandate.engine_config.config_id(),
                                      mandate.policy.policy_id(), mandate.mandate_id())
        cid = register_candidate(self.store, identity, session_id=sid, plan_id=plan_id, actor="controller",
                                 evidence={"dev_experiment": rec["id"], "desk_run": meta["run_id"]})
        state = self.store.get_candidate(cid)["state"]
        if state != "development":
            return {"candidate_id": cid, "verdict": f"resubmission ({state})",
                    "reasons": ["identical candidate already evaluated; its first verdict stands"]}
        base = ExperimentRequest(strategy_ref=rec["strategy_ref"], parameters=params, dataset_id=mandate.dataset_id,
                                 split_id=mandate.split_id, engine_config=mandate.engine_config, policy=mandate.policy)
        gate = GateContext(caller="controller", session_id=sid, allow_weak_isolation=self.allow_weak)
        ev = evaluate_candidate(self.paths, self.store, mandate, base, gate=gate, sandbox=self.sandbox,
                                session_id=sid, parent_id=rec["id"],
                                record={"plan_id": plan_id, "artifact": declared["path"]})
        accepted = ev.metrics is not None and ev.verdict == "accepted"
        transition(self.store, cid, "validated" if accepted else "rejected", "controller", ev.evidence(),
                   "desk validation" if ev.metrics is not None else "evaluation did not complete")
        return {"candidate_id": cid, "verdict": ev.verdict, "reasons": ev.reasons, "validation_primary": ev.primary,
                "validation_dsr": (ev.statistics or {}).get("dsr"), "evaluation_experiments": ev.experiments}


def recover_desk_runs(paths: Paths, store: ExperimentStore, cfg: DeskConfig) -> list[dict[str, Any]]:
    """Close out sessions whose desk process died: clean up containers, recover the session, record it."""
    out = []
    loop = ResearchLoop(paths, store, cfg, adapter=None, require_check=False)
    for run_json in sorted(sessions_dir(paths).glob("*/run.json")):
        run_dir = run_json.parent
        if (run_dir / "result.json").exists():
            continue
        meta = json.loads(run_json.read_text())
        removed = cleanup_run(meta["run_id"])
        recover_session(store, meta["session_id"], "desk process died; recovered at startup")
        ws = Path(store.get_session(meta["session_id"])["workspace"])
        if ws.exists() and not (run_dir / "submission").exists():
            freeze_submission(ws, run_dir / "submission")
        mandate = Mandate.model_validate(store.get_mandate(meta["mandate_id"])["body"])
        res = loop.finish(run_dir, meta, mandate, run=None, egress_records=[],
                          harness_error=f"desk process died; cleaned up {removed}", crash=True)
        out.append({"run_id": meta["run_id"], "failure_class": res["failure_class"], "removed": removed})
    _save_state(paths, load_state(paths), current_run=None)
    return out
