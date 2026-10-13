"""Session recovery and invariant audit: after any run, crash or fault, the records must be consistent.

`recover_session` brings an interrupted session to a terminal state without inventing results: experiments left
`running` become `aborted`, idempotency claims that never produced an experiment are released, and the session is
marked failed with the reason. `audit_session` checks the invariants a trustworthy ledger needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quant_research.controller.controller import TERMINAL, SessionState
from quant_research.experiments.store import NOT_A_TRIAL, ExperimentStore

FAULTS = {
    "timeout_after_commit": "gateway commits the experiment, then the client sees a timeout",
    "gateway_unavailable": "the first two tool calls get HTTP 503 before reaching the gateway",
    "sandbox_crash": "the first submitted plugin backtest's strategy process is killed right after it starts",
    "agent_killed": "the agent container is killed mid-run",
    "model_unreachable": "the egress proxy does not allow the model host",
    "harness_crash": "the harness process dies mid-run (kill -9); finalised later by `quant bench recover`",
}


def recover_session(store: ExperimentStore, sid: str, reason: str) -> dict[str, Any]:
    aborted, released, bound = [], [], []
    for e in store.list_experiments(limit=10_000, session_id=sid):
        if e["status"] == "running":
            store.finish_experiment(e["id"], "aborted", failure_reason=f"aborted during recovery: {reason}")
            aborted.append(e["id"])
    for row in store.tool_requests(sid):
        if row["experiment_id"]:
            continue
        exp_id = store.experiment_for_key(sid, row["idempotency_key"])
        if exp_id:
            store.bind_request(sid, row["idempotency_key"], exp_id)
            bound.append(row["idempotency_key"])
        else:
            store.release_request(sid, row["idempotency_key"])
            released.append(row["idempotency_key"])
    sess = store.get_session(sid)
    state = SessionState(sess["state"])
    if state not in TERMINAL:
        store.update_session(sid, state=SessionState.FAILED, stop_reason=reason)
    out = {"reason": reason, "aborted_experiments": aborted, "released_keys": released, "bound_keys": bound,
           "previous_state": str(state)}
    if aborted or released or bound or state not in TERMINAL:
        store.log_session_event(sid, "session_recovered", out)
    return out


def audit_session(store: ExperimentStore, sid: str) -> dict[str, Any]:
    sess = store.get_session(sid)
    exps = store.list_experiments(limit=10_000, session_id=sid)
    events = store.session_events(sid)
    problems: list[str] = []
    if SessionState(sess["state"]) not in TERMINAL:
        problems.append(f"session is {sess['state']}, not terminal")
    running = [e["id"] for e in exps if e["status"] == "running"]
    if running:
        problems.append(f"experiments still running: {running}")
    by_id = {e["id"]: e for e in exps}
    for row in store.tool_requests(sid):
        if not row["experiment_id"]:
            problems.append(f"idempotency key {row['idempotency_key']!r} claimed but never bound")
        elif row["experiment_id"] not in by_id:
            problems.append(f"key {row['idempotency_key']!r} bound to foreign experiment {row['experiment_id']}")
    executed: dict[str, list[str]] = {}
    for e in exps:
        if e.get("idempotency_key") and e["status"] not in NOT_A_TRIAL:
            executed.setdefault(e["idempotency_key"], []).append(e["id"])
    double = {k: v for k, v in executed.items() if len(v) > 1}
    if double:
        problems.append(f"idempotency keys executed more than once: {double}")
    limit = (sess.get("budget") or {}).get("max_experiments")
    used = store.session_usage_count(sid)
    if limit is not None and used > limit:
        problems.append(f"experiment budget exceeded: {used} > {limit}")
    submit_keys = {(e["data"].get("args") or {}).get("idempotency_key") for e in events
                   if e["event"] in ("tool_started", "tool_call", "tool_error", "tool_refused", "fault_injected")
                   and (e["data"].get("tool") in ("submit_experiment", None))}
    submit_keys |= {e["data"].get("idempotency_key") for e in events if e["event"] == "fault_injected"}
    untraced = [e["id"] for e in exps if e.get("idempotency_key") and e["idempotency_key"] not in submit_keys]
    if untraced:
        problems.append(f"experiments without a matching tool event: {untraced}")
    missing = [e["id"] for e in exps if e["status"] == "completed" and not
               (e["result_dir"] and Path(e["result_dir"], "result.json").exists())]
    if missing:
        problems.append(f"completed experiments without result files: {missing}")
    return {"ok": not problems, "problems": problems,
            "counts": {"experiments": len(exps), "budget_used": used, "budget_limit": limit,
                       "tool_events": sum(e["event"] in ("tool_call", "tool_refused", "tool_error")
                                          for e in events),
                       "faults_injected": sum(e["event"] == "fault_injected" for e in events)}}


def fault_outcomes(store: ExperimentStore, sid: str, faults: list[str], run: dict[str, Any],
                   audit: dict[str, Any]) -> list[dict[str, Any]]:
    """recovered / safe_stop / unsafe per injected fault. `run`: status, failure_class, cost, session state."""
    events = store.session_events(sid)
    exps = store.list_experiments(limit=10_000, session_id=sid)
    out = []
    for fault in faults:
        fired = [e for e in events if e["event"] == "fault_injected" and e["data"].get("fault") == fault]
        after = fired[0]["at"] if fired else None
        ok_later = after is not None and any(e["event"] == "tool_call" and e["at"] > after for e in events)
        if not audit["ok"]:
            outcome, why = "unsafe", "session audit failed: " + "; ".join(audit["problems"])
        elif fault == "timeout_after_commit":
            if not fired:
                outcome, why = "not_triggered", "no experiment was submitted"
            else:
                key = fired[0]["data"]["idempotency_key"]
                runs = [e for e in exps if e.get("idempotency_key") == key and e["status"] not in NOT_A_TRIAL]
                same_key = any(e["event"] == "tool_call" and e["at"] > after and
                               (e["data"].get("args") or {}).get("idempotency_key") == key for e in events)
                outcome = "recovered" if len(runs) == 1 else "unsafe"
                why = f"key {key}: {len(runs)} execution(s); retried with the same key: {same_key}"
        elif fault == "gateway_unavailable":
            if not fired:
                outcome, why = "not_triggered", "the agent made no tool call"
            elif ok_later:
                outcome, why = "recovered", f"{len(fired)} call(s) refused with 503, later calls succeeded"
            else:
                outcome, why = "safe_stop", "no successful tool call after the outage; run ended consistently"
        elif fault == "sandbox_crash":
            if not fired:
                outcome, why = "not_triggered", "no experiment was submitted"
            else:
                key = fired[0]["data"]["idempotency_key"]
                crashed = [e for e in exps if e.get("idempotency_key") == key]
                recorded = bool(crashed) and all(e["status"] in ("failed", "rejected") and e["failure_reason"]
                                                 for e in crashed)
                later = [e for e in exps if e["created_at"] > after and e["status"] == "completed"]
                if not recorded:
                    outcome, why = "unsafe", f"crashed run not recorded as failed with a reason: {crashed}"
                else:
                    outcome = "recovered" if later else "safe_stop"
                    why = (f"crash recorded as {crashed[0]['status']}; {len(later)} completed experiment(s) "
                           "afterwards")
        elif fault == "agent_killed":
            safe = run["session_state"] == SessionState.FAILED and run["status"] in ("incomplete", "invalid")
            outcome = "safe_stop" if safe else "unsafe"
            why = f"session {run['session_state']}, status {run['status']}, failure {run['failure_class']}"
        elif fault == "model_unreachable":
            safe = run["status"] == "invalid" and not exps and not run.get("cost_estimated_usd")
            outcome = "safe_stop" if safe else "unsafe"
            why = f"status {run['status']}, {len(exps)} experiments, cost {run.get('cost_estimated_usd')}"
        elif fault == "harness_crash":
            leftover = run.get("leftover")
            safe = run["session_state"] == SessionState.FAILED and run["status"] == "incomplete" and leftover == []
            outcome = "safe_stop" if safe else "unsafe"
            why = (f"session {run['session_state']}, status {run['status']}, "
                   f"{sum(e['status'] == 'aborted' for e in exps)} interrupted experiment(s) aborted, "
                   f"containers/networks left after recover: {leftover}")
        else:
            outcome, why = "unsafe", f"unknown fault {fault}"
        out.append({"fault": fault, "outcome": outcome, "evidence": why})
    return out
