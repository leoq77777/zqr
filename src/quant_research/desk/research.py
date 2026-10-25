"""The desk's research loop: one isolated agent session at a time, taken from the campaign queue (design 6.3, D4).

Each step: the running campaign, or the next queued one (generator campaigns are auto-queued when fewer than
`QUEUE_FLOOR` wait; inbox campaigns still need the human) -> the budget reservation against the campaign, the
UTC day and the month (D7) -> a fresh session on the
product's real-data mandate (agent tools see `dev` only) with the campaign brief -> agent container behind the M5
egress proxy and tool gateway -> freeze the workspace -> audit -> book the cost -> the session's notes go to the
campaign; a registered hypothesis goes through the gates of release 4c; a written conclusion, or a cap reached,
ends the campaign. Every session leaves `desk/sessions/<run_id>/result.json` and a line in
`desk/research_ledger.jsonl`. Single candidates are no longer evaluated or promoted from the loop (D8).

Guards: the reservation caps, a per-session wall clock and experiment allowance, exponential backoff on failures,
a pause with the reason shown in `desk status` when the environment (model, Docker) is at fault, and the budget
stop (an unpriced model call, or a session costing more than its reservation) until the human clears it.
Idle and cap waits sleep until `resume_at`, so a late slice or a jumped clock does not keep the loop in yesterday.
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
from quant_research.bench.runner import TRANSCRIPT_DIR, freeze_submission
from quant_research.bench.tasks import CONTRACT_V02, benchmark_dir
from quant_research.campaign import budget
from quant_research.campaign.model import VERDICTS, Conclusion
from quant_research.campaign.store import QUEUE_FLOOR, CampaignStore
from quant_research.controller.agent import ManualRunner, usage_from_transcripts
from quant_research.controller.controller import ResearchController, SessionConfig, SessionState
from quant_research.desk import softstop
from quant_research.desk.config import DeskConfig, effective_network, load_check
from quant_research.desk.supervisor import CREDENTIAL_FILE, read_env_file
from quant_research.experiments.store import ExperimentStore
from quant_research.gateway.mcp_server import GatewayHTTPServer
from quant_research.inbox.store import InboxStore
from quant_research.paths import Paths
from quant_research.research.mandate import Mandate
from quant_research.runtime import version
from quant_research.strategy.sandbox import SandboxConfig

log = logging.getLogger(__name__)

ENV_FAILURES = {"environment.model_unreachable", "environment.docker", "harness.error"}
INBOX_TOPIC = "inbox"
NO_TOOLS = {"mcpServers": {}}
PROMPT = ("Complete the research campaign specified in /workspace/TASK.md. Follow /workspace/CLAUDE.md and use "
          "only the `quant` MCP tools for experiments. Write all deliverables under /workspace and validate "
          "research.json with the write_report tool before you finish.")
CAMPAIGN_FIELDS = {
    "notes": {"type": "string", "minLength": 1,
              "description": "What this session did and found, and what the next session should do."},
    "conclusion": {"description": "null to continue the campaign; an object to end it.", "oneOf": [
        {"type": "null"},
        {"type": "object", "additionalProperties": False, "required": ["verdict", "summary"],
         "properties": {"verdict": {"enum": list(VERDICTS)}, "summary": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                        "open_questions": {"type": "array", "items": {"type": "string"}}}}]},
}


def sessions_dir(paths: Paths) -> Path:
    return paths.desk / "sessions"


def inbox_dir(paths: Paths) -> Path:
    return paths.desk / "inbox"


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


def spent_in_month(paths: Paths, month: str) -> float:
    return sum(r.get("cost_usd") or 0.0 for r in read_ledger(paths) if (r.get("day") or "").startswith(month))


def load_state(paths: Paths) -> dict[str, Any]:
    p = state_path(paths)
    return json.loads(p.read_text()) if p.exists() else {"sessions_started": 0, "consecutive_failures": 0}


def _save_state(paths: Paths, state: dict[str, Any], **update: Any) -> dict[str, Any]:
    state.update(update, updated_at=time.time())
    tmp = state_path(paths).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(state_path(paths))
    return state


def _next_day(now: float) -> float:
    today = datetime.fromtimestamp(now, tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return (today + timedelta(days=1)).timestamp() - now + 1


def _next_month(now: float) -> float:
    d = datetime.fromtimestamp(now, tz=UTC)
    first = datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=UTC)
    return first.timestamp() - now + 1


class ResearchLoop:
    def __init__(self, paths: Paths, store: ExperimentStore, cfg: DeskConfig, adapter, *,
                 sandbox: SandboxConfig | None = None, allow_weak_isolation: bool = False,
                 isolate_network: bool = True, require_check: bool = True, drafter=None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        """`drafter` runs inbox drafting (no tools, its own prompt); without one, inbox items wait."""
        self.paths, self.store, self.cfg, self.adapter, self.drafter = paths, store, cfg, adapter, drafter
        self.sandbox = sandbox or SandboxConfig(isolation="docker")
        self.allow_weak = allow_weak_isolation
        self.isolate_network, self.require_check = isolate_network, require_check
        self.clock, self.sleep = clock, sleep
        self.stop_requested = False
        self.adapter_model = getattr(adapter, "model", None)
        sessions_dir(paths).mkdir(parents=True, exist_ok=True)
        self.state = load_state(paths)
        self.campaigns = CampaignStore(store)
        self.inbox = InboxStore(store)

    # -- scheduling ------------------------------------------------------------------------------
    def _wait(self, status: str, reason: str, seconds: float) -> dict[str, Any]:
        self.state = _save_state(self.paths, self.state, status=status, reason=reason,
                                 resume_at=self.clock() + seconds)
        return {"ran": False, "status": status, "reason": reason, "wait": seconds}

    def _session_model(self, camp: dict[str, Any], lim: dict[str, float]) -> tuple[str | None, float]:
        """Campaign body model, else design_model on session 1, else the desk default (A13.11)."""
        explicit = camp["body"].get("model")
        finished = sum(1 for s in self.campaigns.sessions(camp["id"]) if s.get("finished_at"))
        use_design = explicit is None and finished == 0 and self.cfg.design_model
        model = explicit or (self.cfg.design_model if use_design else budget.default_model(self.cfg))
        reserve = self.cfg.design_session_reserve_usd if use_design else lim["session_reserve_usd"]
        return model, reserve

    def _replenish_queue(self, now: float) -> None:
        """Keep at least QUEUE_FLOOR generator campaigns queued while the daily session cap allows (A13.11).
        Inbox and human-added campaigns stay proposed until the human approves them."""
        from quant_research.campaign.generate import generate, unanswerable_open_question

        lim, day = budget.limits(self.cfg), utc_day(now)
        if sum(r.get("day") == day for r in read_ledger(self.paths) if r.get("topic") != INBOX_TOPIC) \
                >= lim["sessions_per_day"]:
            return
        if len(self.campaigns.list(state="queued")) >= QUEUE_FLOOR:
            return
        for product in self.cfg.products:
            if product not in self.cfg.mandates:
                continue
            try:
                generate(self.paths, self.store, product, n=QUEUE_FLOOR)
            except Exception:  # noqa: BLE001 - no reference or quota; try the other product
                continue
        for row in self.campaigns.list(state="proposed"):
            if row["source"].get("kind") != "generator":
                continue
            if unanswerable_open_question(row["body"].get("goal") or ""):
                continue
            if len(self.campaigns.list(state="queued")) >= QUEUE_FLOOR:
                return
            self.campaigns.transition(row["id"], "queued", "controller", "auto-replenish")

    def _cap_reason(self, cid: str) -> str | None:
        """Why the campaign cannot run another session (P1, P3), if it cannot."""
        rem = self.campaigns.remaining(cid)
        if rem["sessions"] <= 0:
            return "session cap reached"
        if rem["usd"] < self.cfg.session_reserve_usd:
            return "dollar cap reached"
        if rem["experiments"] <= 0:
            return "trial cap reached"
        row = self.campaigns.require(cid)
        q = self.campaigns.quota(self.paths, row["split_id"], row["family"])
        if q["room"] <= 0:
            return f"family quota reached ({q['used']} of {q['quota']:.0f} trials)"
        return None

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
        if (stop := budget.active_stop(self.paths)) is not None:
            return self._wait("budget_stop", f"budget stop: {stop['reason']} (human: `zqr campaign budget clear "
                                             "--note`)", 600)
        resume_at = self.state.get("resume_at") or 0
        if self.state.get("status") in ("paused", "backoff") and now < resume_at:
            return {"ran": False, "status": self.state["status"], "reason": self.state.get("reason"),
                    "wait": resume_at - now}
        from quant_research.portfolio.paper import ensure_admitted_paper
        ensure_admitted_paper(self.paths, self.store)
        camp = self.campaigns.running()
        if camp is not None and self._pending_gates(camp["id"]):
            return self._settled(camp["id"], self._settle(camp["id"]))
        if (item := self._inbox_item(now)) is not None:
            return self._after(self.run_inbox(item, budget.default_model(self.cfg), self.cfg.inbox_reserve_usd))
        self._replenish_queue(now)
        camp = camp or self.campaigns.next_queued()
        if camp is None:
            return self._wait("idle", "no running or queued campaign (generator found nothing that fits the quota)",
                              600)
        cid = camp["id"]
        mid = self.cfg.mandates.get(camp["product"])
        if mid is None:
            return self._wait("blocked", f"{cid} is for {camp['product']}, which has no desk mandate", 600)
        mandate = Mandate.model_validate(self.store.get_mandate(mid)["body"])
        if mandate.split_id != camp["split_id"]:
            return self._wait("blocked", f"{cid} is for {camp['split_id']} but the desk mandate is on "
                                         f"{mandate.split_id}; the human cancels it", 600)
        if camp["state"] == "queued":
            self.campaigns.transition(cid, "running", "controller")
        if (reason := self._cap_reason(cid)) is not None:
            return self._settled(cid, self._settle(cid, reason))
        lim, day = budget.limits(self.cfg), utc_day(now)
        model, reserve = self._session_model(camp, lim)
        spent = spent_on(self.paths, day)
        if spent + reserve > lim["daily_usd"]:
            return self._wait("cost_cap", f"daily cap: {spent:.2f} booked + {reserve:.2f} reserved > "
                                          f"{lim['daily_usd']} USD", _next_day(now))
        month = spent_in_month(self.paths, budget.month_of(day))
        if month + reserve > lim["monthly_usd"]:
            return self._wait("cost_cap", f"monthly cap: {month:.2f} booked + {reserve:.2f} reserved > "
                                          f"{lim['monthly_usd']} USD", _next_month(now))
        if sum(r.get("day") == day for r in read_ledger(self.paths) if r.get("topic") != INBOX_TOPIC) \
                >= lim["sessions_per_day"]:
            return self._wait("cost_cap", f"sessions per day ({lim['sessions_per_day']:.0f}) reached", 3600)
        if model is None:
            return self._wait("blocked", "no model configured (desk config `model` or ANTHROPIC_MODEL)", 600)
        if budget.price_for(self.cfg, [model]) is None:
            return self._wait("blocked", f"model {model} has no price; add it to desk config `campaign_models`",
                              600)
        return self._after(self.run_session(camp, mandate, model, reserve))

    def _after(self, res: dict[str, Any]) -> dict[str, Any]:
        failure = res["failure_class"]
        n = self.state.get("consecutive_failures", 0) + 1 if failure != "none" else 0
        if failure == "desk.stopped":
            return {"ran": True, "status": "stopping", "result": res}
        if (res.get("campaign") or {}).get("gates_error"):
            self.state = _save_state(self.paths, self.state, status="paused", consecutive_failures=n,
                                     reason=f"gates: {res['campaign']['gates_error'][:300]}",
                                     resume_at=self.clock() + self.cfg.backoff_max_seconds)
        elif failure in ENV_FAILURES:
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
            deadline = self.state.get("resume_at")
            if deadline is None:
                deadline = self.clock() + float(out.get("wait") or 0)
            while not self.stop_requested and self.clock() < deadline:
                self.sleep(min(5.0, max(0.0, deadline - self.clock())))
        self.state = _save_state(self.paths, self.state, status="stopped", current_run=None)

    # -- campaign bookkeeping --------------------------------------------------------------------
    def _pending_gates(self, cid: str) -> str | None:
        from quant_research.campaign.conclude import campaign_hypothesis
        from quant_research.hypothesis.store import HypothesisStore

        hid = campaign_hypothesis(self.store, cid)
        if hid is None:
            return None
        hs = HypothesisStore(self.store)
        g1 = hs.event(hid, "gate1")
        return hid if g1 is None or (g1["evidence"]["passed"] and hs.event(hid, "gate2") is None) else None

    def _attach_admitted(self, cid: str, vid: str) -> dict[str, Any]:
        """Shadow by default; super authorization puts the version on official paper now (A13.12)."""
        from quant_research.desk.promotion import authorization_path
        from quant_research.portfolio.paper import start_admitted_paper, start_shadow
        from quant_research.research.authorization import load as load_authorization

        auth = load_authorization(authorization_path(self.paths))
        attached: dict[str, Any] = {}
        starter = start_admitted_paper if auth is not None and auth.auto_paper_admitted else start_shadow
        key, label = ("paper", "admitted auto-paper") if starter is start_admitted_paper else (
            "shadow", "admitted auto-shadow")
        try:
            attached[key] = starter(self.paths, self.store, vid, label, actor="controller")
        except Exception as exc:  # noqa: BLE001 - paper attach is not a gate failure
            attached[f"{key}_error"] = f"{type(exc).__name__}: {exc}"
            self.campaigns.record(cid, f"{key}_error", "controller",
                                  {"version_id": vid, "error": attached[f"{key}_error"][:500]})
        return attached

    def _settle(self, cid: str, cap_reason: str | None = None) -> dict[str, Any]:
        """Gates for a registered hypothesis (with top-ups on passes), then the end of the campaign if a session
        wrote a conclusion or a cap is reached."""
        from quant_research.campaign.conclude import finish_campaign
        from quant_research.hypothesis.gates import run_gates, verdict

        out: dict[str, Any] = {"campaign_id": cid}
        if (hid := self._pending_gates(cid)) is not None:
            try:
                gates = run_gates(self.paths, self.store, hid, self.sandbox)
            except Exception as exc:  # noqa: BLE001 - recorded; the loop pauses and retries the gates later
                msg = f"{type(exc).__name__}: {exc}"
                self.campaigns.record(cid, "gates_error", "controller", {"hypothesis_id": hid, "error": msg[:500]})
                return out | {"gates_error": msg}
            out["gates"] = verdict(self.store, hid)
            self.campaigns.record(cid, "gates", "controller", out["gates"])
            if gates["gate1"]["passed"]:
                self.campaigns.topup(cid, "gate1")
            if gates["state"] == "admitted":
                self.campaigns.topup(cid, "gate2")
                vid = (gates.get("gate2") or {}).get("version_id")
                if vid:
                    out.update(self._attach_admitted(cid, vid))
        written = self.campaigns.event(cid, "conclusion_written")
        if written is not None:
            out["end"] = finish_campaign(self.paths, self.store, cid, "concluded",
                                         Conclusion.model_validate(written["data"]["conclusion"]),
                                         f"written by session {written['data']['session_id']}")
        elif (reason := cap_reason or self._cap_reason(cid)) is not None:
            out["end"] = finish_campaign(self.paths, self.store, cid, "exhausted", None, reason)
        return out

    def _settled(self, cid: str, out: dict[str, Any]) -> dict[str, Any]:
        if out.get("gates_error"):
            return self._wait("paused", f"gates of {cid}: {out['gates_error'][:300]}", self.cfg.backoff_max_seconds)
        return {"ran": False, "status": "running", "reason": f"campaign {cid} settled", "wait": 0, "campaign": out}

    # -- one session -----------------------------------------------------------------------------
    def _workspace(self, ws: Path, brief: str) -> None:
        root = benchmark_dir()
        (ws / "fixtures").mkdir(exist_ok=True)
        shutil.copyfile(root / "role_prompt.md", ws / "CLAUDE.md")
        schema = json.loads((root / "v0.2" / "research.schema.json").read_text())
        schema["properties"] |= CAMPAIGN_FIELDS
        schema["required"] = [*schema["required"], "notes"]
        (ws / "research.schema.json").write_text(json.dumps(schema, indent=2))
        shutil.copyfile(root / CONTRACT_V02, ws / "fixtures" / "strategy_contract.md")
        (ws / "TASK.md").write_text(brief)

    def run_session(self, camp: dict[str, Any], mandate: Mandate, model: str, reserve: float) -> dict[str, Any]:
        from quant_research.campaign.brief import session_brief

        cid = camp["id"]
        n = self.state.get("sessions_started", 0)
        rem = self.campaigns.remaining(cid)
        b = mandate.budget.max_experiments
        allowance = min(b, self.campaigns.trials_left(self.paths, cid))
        last = rem["sessions"] <= 1 or rem["usd"] - reserve < reserve
        run_id = f"desk-{datetime.fromtimestamp(self.clock(), tz=UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        brief = session_brief(self.paths, self.store, cid, run_id, mandate, allowance, last)
        sess_cfg = SessionConfig(mandate=mandate, sandbox=self.sandbox, allow_weak_isolation=self.allow_weak)
        sid = ResearchController(self.paths, self.store, ManualRunner()).create_session(sess_cfg)
        self.campaigns.start_session(cid, sid, run_id, allowance, reserve, last)
        ws = Path(self.store.get_session(sid)["workspace"])
        self._workspace(ws, brief)
        run_dir = sessions_dir(self.paths) / run_id
        run_dir.mkdir(parents=True)
        meta = {"run_id": run_id, "session_id": sid, "product": camp["product"], "topic": "campaign",
                "campaign_id": cid, "family": camp["family"], "model": model, "reserved_usd": reserve,
                "mandate_id": mandate.mandate_id(), "started_at": self.clock()}
        (run_dir / "run.json").write_text(json.dumps(meta, indent=2))
        self.state = _save_state(self.paths, self.state, sessions_started=n + 1, current_run=run_id,
                                 status="running", reason=f"session {run_id} ({cid}, {camp['family']})")
        if hasattr(self.adapter, "model"):
            self.adapter.model = model or self.adapter_model
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
        return self.finish(run_dir, meta, run=run, egress_records=egress_records, harness_error=harness_error)

    def finish(self, run_dir: Path, meta: dict[str, Any], *, run: AgentRun | None,
               egress_records: list[dict[str, Any]], harness_error: str | None, crash: bool = False) -> dict[str, Any]:
        from quant_research.campaign.conclude import parse_conclusion

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
        u = (run.usage if run else None) or usage_from_transcripts(run_dir / TRANSCRIPT_DIR)
        cost, cost_source = self._cost(run, u, meta.get("reserved_usd"),
                                       self._maybe_called(run_dir, egress_records, crash), meta["started_at"])
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
        n_exp = sum(e["status"] != "blocked" for e in exps
                    if e["kind"] not in ("sensitivity",) and e["segment"] == "dev")
        models = (", ".join(run.extra.get("models") or []) if run else None) or (u.model if u else None)
        reserved = meta.get("reserved_usd")
        self._book_stop(meta["run_id"], cost, cost_source, reserved, models)
        finished = self.clock()
        result = {
            "run_id": meta["run_id"], "session_id": sid, "product": meta["product"], "topic": meta["topic"],
            "campaign_id": meta.get("campaign_id"), "family": meta.get("family"),
            "mandate_id": meta["mandate_id"], "started_at": meta["started_at"], "finished_at": finished,
            "day": utc_day(meta["started_at"]), "wall_seconds": round(finished - meta["started_at"], 1),
            "status": "ok" if failure == "none" else "failed", "failure_class": failure,
            "failure_details": [str(d) for d in details if d][:5],
            "cost_usd": cost, "cost_source": cost_source, "reserved_usd": reserved,
            "usage": u.model_dump() if u else None, "zqr_version": version(),
            "harness": run.harness_version if run else None, "model": models, "experiments": n_exp,
            "audit_ok": audit["ok"], "audit_problems": audit["problems"], "violations": violations,
            "egress": egress,
        }
        cid = meta.get("campaign_id")
        if cid:
            conclusion, problems = parse_conclusion(research) if failure == "none" else (None, [])
            notes = (research or {}).get("notes")
            self.campaigns.finish_session(sid, cost, n_exp, notes if isinstance(notes, str) else None, failure)
            if conclusion is not None:
                self.campaigns.record(cid, "conclusion_written", "agent_tool",
                                      {"session_id": sid, "conclusion": conclusion.model_dump(mode="json")})
            result["campaign"] = ({"campaign_id": cid, "conclusion_problems": problems} if crash
                                  else self._settle(cid) | {"conclusion_problems": problems})
        (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        camp = result.get("campaign") or {}
        with open(ledger_path(self.paths), "a") as f:
            f.write(json.dumps({k: result.get(k) for k in (
                "run_id", "day", "product", "topic", "campaign_id", "family", "status", "failure_class", "cost_usd",
                "cost_source", "reserved_usd", "experiments", "audit_ok", "session_id", "zqr_version", "harness",
                "model")}
                | {"violations": len(violations), "gates": (camp.get("gates") or {}).get("state"),
                   "campaign_end": (camp.get("end") or {}).get("state")}, default=str) + "\n")
        self.state = _save_state(self.paths, self.state, current_run=None, last_run_id=meta["run_id"])
        return result

    def _book_stop(self, run_id: str, cost: float, cost_source: str, reserved: float | None,
                   models: str | None) -> None:
        if cost_source == "unpriced":
            budget.stop(self.paths, f"model call without a configured price ({models}); booked {cost:.4f} USD",
                        run_id=run_id, cost_usd=cost)
        elif cost_source == "cost_unknown":
            budget.stop(self.paths, f"run {run_id} reported no usage; booked at its reservation {cost:.2f} USD until "
                                    "the human reconciles it", run_id=run_id, cost_usd=cost)
        elif reserved is not None and cost > reserved:
            budget.stop(self.paths, f"session {run_id} cost {cost:.4f} USD, above its reservation "
                                    f"{reserved:.2f} USD", run_id=run_id, cost_usd=cost)

    # -- inbox drafting --------------------------------------------------------------------------
    def _inbox_item(self, now: float) -> dict[str, Any] | None:
        """The oldest received item, if a drafting run fits the daily limit, the budget and a priced model; otherwise
        items wait and campaigns go on (their own checks report a cap or a missing model)."""
        if self.drafter is None or (item := self.inbox.next_received()) is None:
            return None
        day, lim, reserve = utc_day(now), budget.limits(self.cfg), self.cfg.inbox_reserve_usd
        drafts = sum(r.get("day") == day and r.get("topic") == INBOX_TOPIC for r in read_ledger(self.paths))
        if drafts >= self.cfg.inbox_drafts_per_day:
            return None
        if spent_on(self.paths, day) + reserve > lim["daily_usd"] or \
                spent_in_month(self.paths, budget.month_of(day)) + reserve > lim["monthly_usd"]:
            return None
        model = budget.default_model(self.cfg)
        if model is None or budget.price_for(self.cfg, [model]) is None:
            return None
        return item

    def run_inbox(self, item: dict[str, Any], model: str, reserve: float) -> dict[str, Any]:
        """One drafting run: the agent container behind the egress proxy (model endpoint only), no tools."""
        from quant_research.inbox.draft import write_workspace

        iid = item["id"]
        run_id = f"inbox-{datetime.fromtimestamp(self.clock(), tz=UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        run_dir = inbox_dir(self.paths) / run_id
        run_dir.mkdir(parents=True)
        self.inbox.transition(iid, "drafting", "controller", run_id=run_id)
        meta = {"run_id": run_id, "inbox_id": iid, "product": item["product"] or "-", "topic": INBOX_TOPIC,
                "model": model, "reserved_usd": reserve, "started_at": self.clock()}
        (run_dir / "run.json").write_text(json.dumps(meta, indent=2))
        self.state = _save_state(self.paths, self.state, current_run=run_id, status="running",
                                 reason=f"inbox draft {run_id} ({iid})")
        run: AgentRun | None = None
        harness_error, egress_records = None, []
        try:
            write_workspace(self.paths, self.store, self.cfg, item, run_dir / "workspace")
            if self.isolate_network:
                with EgressNetwork(run_dir, [model_endpoint()], None, label=run_id) as net:
                    run = self.drafter.run(run_dir / "workspace", NO_TOOLS, self.cfg.inbox_wall_seconds, network=net,
                                           label=run_id, transcript_dir=run_dir / TRANSCRIPT_DIR)
                    egress_records = net.records()
            else:
                run = self.drafter.run(run_dir / "workspace", NO_TOOLS, self.cfg.inbox_wall_seconds)
        except Exception as exc:  # noqa: BLE001 - every failure still ends in a record and the item's state
            harness_error = f"{type(exc).__name__}: {exc}"
            (run_dir / "harness_error.txt").write_text(traceback.format_exc())
            cleanup_run(run_id)
        return self.finish_inbox(run_dir, meta, run=run, egress_records=egress_records, harness_error=harness_error)

    def finish_inbox(self, run_dir: Path, meta: dict[str, Any], *, run: AgentRun | None,
                     egress_records: list[dict[str, Any]], harness_error: str | None,
                     crash: bool = False) -> dict[str, Any]:
        from quant_research.inbox.draft import read_draft, write_campaigns

        iid = meta["inbox_id"]
        egress = summarize(egress_records) if (self.isolate_network and egress_records) else None
        u = (run.usage if run else None) or usage_from_transcripts(run_dir / TRANSCRIPT_DIR)
        cost, cost_source = self._cost(run, u, meta.get("reserved_usd"),
                                       self._maybe_called(run_dir, egress_records, crash), meta["started_at"])
        draft, problem = read_draft(run_dir / "workspace")
        no_model = run is None or run.usage is None or (run.usage.input_tokens + run.usage.output_tokens) == 0
        if crash:
            failure, details = "harness.crash", [harness_error or "desk process died during the run"]
        elif harness_error:
            failure, details = "harness.error", [harness_error]
        elif no_model and egress is not None and egress["model_tunnels"] == 0:
            failure, details = "environment.model_unreachable", egress["denied"][:3] + egress["upstream_errors"][:3]
        elif run is not None and run.timed_out:
            failure, details = "agent.timeout", ["wall clock exceeded"]
        elif no_model:
            failure, details = "environment.docker", [(run.stderr_tail if run else "")[-400:]]
        elif draft is None:
            failure, details = "agent.no_deliverable", [problem]
        else:
            failure, details = "none", []
        if self.stop_requested and failure != "none":
            failure, details = "desk.stopped", ["desk stop requested during the run", *details]
        model_host = {h for h, _ in [model_endpoint()]}
        violations = [f"[network] {d}" for d in (egress or {}).get("denied", [])
                      if d.split(" ", 1)[-1].split(":", 1)[0] not in model_host]
        models = (", ".join(run.extra.get("models") or []) if run else None) or (u.model if u else None)
        self._book_stop(meta["run_id"], cost, cost_source, meta.get("reserved_usd"), models)
        self.inbox.add_cost(iid, cost)
        written: dict[str, Any] = {"campaign_ids": [], "refused": []}
        if self.inbox.require(iid)["state"] == "drafting":
            if failure == "none" and draft.verdict == "infeasible":
                self.inbox.transition(iid, "infeasible", "controller", draft=draft.model_dump(mode="json"),
                                      reason=draft.reason, run_id=meta["run_id"])
            elif failure == "none":
                written = write_campaigns(self.paths, self.store, self.cfg, self.inbox.require(iid), draft)
                self.inbox.transition(iid, "drafted", "controller",
                                      draft=draft.model_dump(mode="json") | {"refused": written["refused"],
                                                                             "published_used": written["published"]},
                                      campaign_ids=written["campaign_ids"],
                                      reason="; ".join(r["reason"] for r in written["refused"]) or None,
                                      run_id=meta["run_id"])
            elif failure in ENV_FAILURES or failure in ("harness.crash", "desk.stopped"):
                self.inbox.transition(iid, "received", "controller", reason=f"{failure}; will be drafted again",
                                      run_id=meta["run_id"])
            else:
                self.inbox.transition(iid, "failed", "controller", reason=f"{failure}: {'; '.join(details)}"[:500],
                                      run_id=meta["run_id"])
        finished = self.clock()
        result = {
            "run_id": meta["run_id"], "inbox_id": iid, "product": meta["product"], "topic": INBOX_TOPIC,
            "campaign_id": None, "family": None, "started_at": meta["started_at"], "finished_at": finished,
            "day": utc_day(meta["started_at"]), "wall_seconds": round(finished - meta["started_at"], 1),
            "status": "ok" if failure == "none" else "failed", "failure_class": failure,
            "failure_details": [str(d) for d in details if d][:5], "cost_usd": cost, "cost_source": cost_source,
            "reserved_usd": meta.get("reserved_usd"), "usage": u.model_dump() if u else None,
            "zqr_version": version(), "harness": run.harness_version if run else None, "model": models,
            "experiments": 0, "audit_ok": True, "violations": violations, "egress": egress,
            "inbox_state": self.inbox.require(iid)["state"], "campaigns_written": written["campaign_ids"],
        }
        (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        with open(ledger_path(self.paths), "a") as f:
            f.write(json.dumps({k: result.get(k) for k in (
                "run_id", "day", "product", "topic", "campaign_id", "family", "status", "failure_class", "cost_usd",
                "cost_source", "reserved_usd", "experiments", "audit_ok", "zqr_version", "harness", "model",
                "inbox_id", "inbox_state", "campaigns_written")}
                | {"session_id": None, "violations": len(violations), "gates": None, "campaign_end": None},
                default=str) + "\n")
        self.state = _save_state(self.paths, self.state, current_run=None, last_run_id=meta["run_id"])
        return result

    def _cost(self, run: AgentRun | None, u, reserved: float | None = None,
              maybe_called: bool = True, started: float | None = None) -> tuple[float, str]:
        """Booked cost and its source. Usage is priced over the run's whole span (peak if any part of it is), since
        the time of each call is unknown. Without a price or without any usage report, a run that may have called the
        model is booked at the CLI's estimate or else its reservation, and `_book_stop` stops the loop."""
        models = (run.extra.get("models") if run else None) or (u.model.split(",") if u and u.model else [])
        pricing = budget.price_for(self.cfg, models)
        if u and pricing:
            now = self.clock()
            cost = pricing.cost(u.input_tokens, u.output_tokens, u.cache_read_tokens,
                                ts=now if started is None else started, until=now)
            return round(cost, 6), "priced_usage"
        if u is not None and (u.input_tokens + u.output_tokens) == 0:
            return 0.0, "no_tokens"
        estimate = run.cli_cost_usd if run is not None else None
        booked = round(float(estimate if estimate is not None else reserved or 0.0), 6)
        if u is None and estimate is None:
            return (booked, "cost_unknown") if maybe_called else (0.0, "no_tokens")
        return booked, "unpriced"

    def _maybe_called(self, run_dir: Path, egress_records: list[dict[str, Any]], crash: bool) -> bool:
        """Whether a run without a usage report may have called the model. An isolated run that ended normally has
        the proxy's full record; otherwise (no isolation, or a crash with tunnels still open, which the proxy logs
        only when they close) any transcript or logged tunnel counts."""
        if self.isolate_network and not crash:
            return summarize(egress_records)["model_tunnels"] > 0
        log = run_dir / "egress.jsonl"
        logged = [json.loads(x) for x in log.read_text().splitlines() if x.strip()] if log.is_file() else []
        transcripts = run_dir / TRANSCRIPT_DIR
        return summarize(logged)["model_tunnels"] > 0 or (transcripts.is_dir() and any(transcripts.rglob("*")))

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


def recover_desk_runs(paths: Paths, store: ExperimentStore, cfg: DeskConfig) -> list[dict[str, Any]]:
    """Close out sessions whose desk process died: clean up containers, recover the session, record it. Gates and
    the end of the campaign wait for the loop's next step."""
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
        res = loop.finish(run_dir, meta, run=None, egress_records=[],
                          harness_error=f"desk process died; cleaned up {removed}", crash=True)
        out.append({"run_id": meta["run_id"], "failure_class": res["failure_class"], "removed": removed})
    for run_json in sorted(inbox_dir(paths).glob("*/run.json")):
        if (run_json.parent / "result.json").exists():
            continue
        meta = json.loads(run_json.read_text())
        removed = cleanup_run(meta["run_id"])
        res = loop.finish_inbox(run_json.parent, meta, run=None, egress_records=[],
                                harness_error=f"desk process died; cleaned up {removed}", crash=True)
        out.append({"run_id": meta["run_id"], "failure_class": res["failure_class"], "removed": removed})
    _save_state(paths, load_state(paths), current_run=None)
    return out
