"""Run benchmark tasks end to end and record a result that can be trusted or is explicitly marked otherwise.

prepare -> gateway + egress network -> agent container -> freeze submission -> recover -> grade -> audit ->
classify -> result.json (benchmark/v0.2/result.schema.json). Any exception still ends in a terminal session, an
audit and a result with `failure_class`; `zqr bench recover` finalises runs whose harness process died.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import jsonschema

from quant_research.bench.adapter import AgentRun
from quant_research.bench.audit import FAULTS, audit_session, fault_outcomes, recover_session
from quant_research.bench.egress import (
    GATEWAY_PORT,
    PROXY_ALIAS,
    EgressNetwork,
    cleanup_run,
    model_endpoint,
    summarize,
)
from quant_research.bench.grader import VIOLATION_SOURCES, Grade, artifact_hashes, grade
from quant_research.bench.tasks import BenchTask, benchmark_dir, load_tasks
from quant_research.bench.workspace import prepare
from quant_research.controller.agent import usage_from_transcripts
from quant_research.controller.budget import ModelPricing, pricing_preset
from quant_research.controller.controller import SessionState
from quant_research.experiments.store import ExperimentStore
from quant_research.gateway.mcp_server import FAULTS as GATEWAY_FAULTS
from quant_research.gateway.mcp_server import GatewayHTTPServer
from quant_research.hashing import hash_obj
from quant_research.paths import Paths
from quant_research.strategy.sandbox import SandboxConfig

PASS_SCORE = 0.6
KILL_AFTER_S = 45  # fault agent_killed
RESULT_SCHEMA = "v0.2/result.schema.json"
TRANSCRIPT_DIR = "agent_transcript"
UNAVAILABLE = ["cost_billed_usd: vendor bill not visible to the harness",
               "model_requests: the CLI reports agent turns, not HTTP requests"]


def _pricing(models: list[str], cny_per_usd: float) -> ModelPricing | None:
    joined = ",".join(models).lower()
    if "deepseek-v4-pro" in joined or "deepseek-pro" in joined:
        return pricing_preset("deepseek-v4-pro", cny_per_usd)
    if "deepseek" in joined:
        return pricing_preset("deepseek-flash", cny_per_usd)
    return None


def _sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def grader_version() -> str:
    src = Path(__file__).parent
    return hash_obj({f: _sha_file(src / f) for f in ("grader.py", "audit.py", "fixtures.py")})[:16]


def freeze_submission(workspace: Path, dest: Path) -> str:
    """Copy the workspace as the agent left it; read-only; returns the manifest hash."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(workspace, dest, symlinks=False)
    manifest = {str(p.relative_to(dest)): _sha_file(p) for p in sorted(dest.rglob("*")) if p.is_file()}
    (dest.parent / "submission_manifest.json").write_text(json.dumps(manifest, indent=2))
    for p in dest.rglob("*"):
        if p.is_file():
            p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return hash_obj(manifest)


def identity(adapter, task: BenchTask, mandate_id: str, allow: list[tuple[str, int]], faults: list[str],
             network: bool) -> dict[str, Any]:
    root = benchmark_dir()
    fixtures = {f: _sha_file(root / "fixtures" / f) for f in task.visible_fixtures
                if (root / "fixtures" / f).is_file()}
    return {"adapter": adapter.name, "image": getattr(adapter, "image", None),
            "image_digest": adapter.image_digest() if hasattr(adapter, "image_digest") else None,
            "harness_version": adapter.harness_version() if hasattr(adapter, "harness_version") else adapter.name,
            "model_flag": getattr(adapter, "model", None), "task": task.id, "task_brief_sha": hash_obj(task.brief),
            "budget": task.budget, "mandate_id": mandate_id, "fixtures": fixtures, "grader_version": grader_version(),
            "research_schema_sha": _sha_file(root / "v0.2" / "research.schema.json"),
            "egress_allow": [f"{h}:{p}" for h, p in allow] if network else None, "faults": sorted(faults)}


def classify(*, run: AgentRun | None, g: Grade | None, audit: dict[str, Any], faults: list[str],
             egress: dict[str, Any] | None, harness_error: str | None, has_research: bool) -> tuple[str, str, list]:
    """(status, failure_class, details). Precedence: harness, audit, environment, agent, grade."""
    details: list[str] = []
    if harness_error:
        return "invalid", "harness.error", [harness_error]
    if not audit["ok"]:
        return "invalid", "audit.failed", audit["problems"]
    no_model = run is None or run.usage is None or (run.usage.input_tokens + run.usage.output_tokens) == 0
    if no_model and egress is not None and egress["model_tunnels"] == 0:
        return "invalid", "environment.model_unreachable", egress["denied"][:5] + egress["upstream_errors"][:5]
    if run is not None and run.timed_out and "agent_killed" in faults:
        return "incomplete", "agent.killed", [f"agent container killed after {KILL_AFTER_S}s (injected fault)"]
    if run is not None and run.timed_out:
        return "incomplete", "agent.timeout", ["wall clock exceeded"]
    if no_model:
        return "invalid", "environment.docker", [(run.stderr_tail if run else "")[-500:]]
    if not has_research:
        cls = "agent.error" if (run.exit_code not in (0, None) or run.is_error) else "agent.no_deliverable"
        return "incomplete", cls, [f"exit {run.exit_code}; is_error {run.is_error}; research.json missing",
                                   run.result_text[-300:]]
    if g.hard_failures:
        return "failed", "grade.hard_failure", g.hard_failures
    if g.critical_misses():
        return "failed", "grade.critical_item", g.critical_misses()
    if g.score() < PASS_SCORE:
        return "failed", "grade.below_threshold", [f"score {g.score():.2f} < {PASS_SCORE}"]
    return "passed", "none", details


def validate_result(result: dict[str, Any]) -> None:
    schema = json.loads((benchmark_dir() / RESULT_SCHEMA).read_text())
    jsonschema.validate(result, schema)


def run_task(paths: Paths, store: ExperimentStore, task_id: str, adapter, *, repeat: int = 1,
             track: str = "product", cny_per_usd: float = 7.1, sandbox: SandboxConfig | None = None,
             allow_weak_isolation: bool = False, faults: list[str] | None = None,
             isolate_network: bool = True) -> dict[str, Any]:
    task = load_tasks()[task_id]
    faults = sorted(set(task.faults) | set(faults or []))
    injectable = set(FAULTS) - {"harness_crash"}  # drilled by killing the harness, then `zqr bench recover`
    unknown = set(faults) - injectable
    if unknown:
        raise ValueError(f"unknown faults {sorted(unknown)}; injectable: {sorted(injectable)}")
    run_id = f"{task_id.lower()}-r{repeat}-{uuid.uuid4().hex[:8]}"
    prep = prepare(paths, store, task, run_id, sandbox, allow_weak_isolation)
    mandate_id = store.get_session(prep.session_id)["config"]["mandate"]
    mandate_id = hash_obj(mandate_id)[:16]
    allow = [] if "model_unreachable" in faults else [model_endpoint()]
    (prep.run_dir / "run.json").write_text(json.dumps({**json.loads((prep.run_dir / "run.json").read_text()),
                                                       "faults": faults, "started_at": time.time()}, indent=2))
    run: AgentRun | None = None
    g: Grade | None = None
    egress_records: list[dict[str, Any]] = []
    harness_error = None
    submission_sha = None
    t0 = time.time()
    try:
        store.update_session(prep.session_id, state=SessionState.RUNNING)
        gw_faults = [f for f in faults if f in GATEWAY_FAULTS]
        timeout = min(task.wall_seconds, KILL_AFTER_S) if "agent_killed" in faults else task.wall_seconds
        with GatewayHTTPServer(paths, store, prep.session_id, faults=gw_faults,
                               extra_hosts=(f"{PROXY_ALIAS}:{GATEWAY_PORT}",), run_label=run_id) as srv:
            if isolate_network:
                with EgressNetwork(prep.run_dir, allow, srv.port, label=run_id) as net:
                    run = adapter.run(prep.workspace, srv.client_config(url=net.gateway_url()), timeout,
                                      network=net, label=run_id, transcript_dir=prep.run_dir / TRANSCRIPT_DIR)
                    egress_records = net.records()
            else:
                run = adapter.run(prep.workspace, srv.client_config(), timeout)
        submission_sha = freeze_submission(prep.workspace, prep.run_dir / "submission")
        if run.timed_out:
            reason = "agent killed (injected fault)" if "agent_killed" in faults else "wall clock exceeded"
            recover_session(store, prep.session_id, reason)
        else:
            store.update_session(prep.session_id, state=SessionState.COMPLETED, stop_reason="benchmark run finished")
            recover_session(store, prep.session_id, "post-run consistency check")
        g = grade(paths, store, task_id, prep.session_id, sandbox, submission=prep.run_dir / "submission")
    except Exception as exc:  # noqa: BLE001 - every failure must still produce a terminal session and a result
        harness_error = f"{type(exc).__name__}: {exc}"
        (prep.run_dir / "harness_error.txt").write_text(traceback.format_exc())
        recover_session(store, prep.session_id, f"harness error: {harness_error[:200]}")
        cleanup_run(run_id)
    return finalize(paths, store, prep.run_dir, task, run_id, prep.session_id, repeat=repeat, track=track,
                    adapter=adapter, run=run, g=g, faults=faults, egress_records=egress_records,
                    network=isolate_network, allow=allow, mandate_id=mandate_id, harness_error=harness_error,
                    submission_sha=submission_sha, cny_per_usd=cny_per_usd, wall=time.time() - t0)


def finalize(paths: Paths, store: ExperimentStore, run_dir: Path, task: BenchTask, run_id: str, sid: str, *,
             repeat: int, track: str, adapter, run: AgentRun | None, g: Grade | None, faults: list[str],
             egress_records: list[dict[str, Any]], network: bool, allow: list[tuple[str, int]], mandate_id: str,
             harness_error: str | None, submission_sha: str | None, cny_per_usd: float, wall: float,
             crash: bool = False, leftover: list[str] | None = None) -> dict[str, Any]:
    audit = audit_session(store, sid)
    egress = summarize(egress_records) if network else None
    research_present = (run_dir / "submission" / "research.json").is_file()
    status, failure, details = classify(run=run, g=g, audit=audit, faults=faults, egress=egress,
                                        harness_error=harness_error, has_research=research_present)
    if crash:
        status, failure, details = "incomplete", "harness.crash", [harness_error or "harness process died"]
    violations = {s: list(g.violations[s]) if g else [] for s in VIOLATION_SOURCES}
    model_host = {h for h, _ in allow} | ({model_endpoint()[0]} if "model_unreachable" in faults else set())
    for d in (egress or {}).get("denied", []):
        host = d.split(" ", 1)[-1].split(":", 1)[0]
        if host not in model_host:
            violations["network"].append(d)
    exps = store.list_experiments(limit=1000, session_id=sid)
    u = run.usage if run else usage_from_transcripts(run_dir / TRANSCRIPT_DIR)
    usage_source = (run.extra.get("usage_source") if run else "transcript") if u else None
    models = (run.extra.get("models") if run else None) or (u.model.split(",") if u and u.model else [])
    pricing = _pricing(models, cny_per_usd)
    no_model_traffic = egress is not None and egress["model_tunnels"] == 0
    no_tokens = u is None or (u.input_tokens + u.output_tokens + u.cache_read_tokens) == 0
    if u and pricing:
        cost = pricing.cost(u.input_tokens, u.output_tokens, u.cache_read_tokens, ts=time.time())
    else:
        cost = 0.0 if no_tokens and no_model_traffic else None
    unavailable = list(UNAVAILABLE) + ([] if cost is not None else
                                       ["cost_estimated_usd: token usage or vendor pricing unavailable"])
    ident = identity(adapter, task, mandate_id, allow, faults, network)
    sess = store.get_session(sid)
    run_view = {"status": status, "failure_class": failure, "cost_estimated_usd": cost,
                "session_state": SessionState(sess["state"]), "leftover": leftover}
    outcomes = fault_outcomes(store, sid, faults, run_view, audit)
    report = run_dir / "submission" / "report.md"
    result = {
        "schema_version": "v0.2", "run_id": run_id, "task_id": task.id, "repeat": repeat, "track": track,
        "adapter": getattr(adapter, "name", "unknown"), "harness_version": ident["harness_version"],
        "model_id": ",".join(models) or "unknown",
        "provider": "deepseek (Anthropic-compatible endpoint)" if pricing else "unknown",
        "config_hash": hash_obj(ident)[:16], "identity": ident, "status": status, "failure_class": failure,
        "failure_details": [str(x) for x in details if x], "score": round(g.score(), 4) if g else None,
        "cost_estimated_usd": round(cost, 6) if cost is not None else None, "cost_billed_usd": None,
        "usage": u.model_dump() if u else None, "usage_source": usage_source, "wall_seconds": round(wall, 2),
        "backtest_seconds": round(sum((e["finished_at"] or e["created_at"]) - e["created_at"] for e in exps), 3),
        "model_requests": None, "experiments_submitted": sum(e["status"] != "blocked" for e in exps),
        "hard_failures": g.hard_failures if g else [],
        "attempted_violations": [f"[{s}] {v}" for s in VIOLATION_SOURCES for v in violations[s]],
        "violations": violations, "network_isolation": "egress_proxy" if network else "none", "audit": audit,
        "faults": outcomes, "submission_sha256": submission_sha,
        "rubric_items": [i.out() for i in g.items] if g else [], "unavailable_fields": unavailable,
        "report_path": str(report) if report.exists() else None,
    }
    validate_result(result)
    detail = {"session_id": sid, "workspace": str(run_dir / "submission"), "score": result["score"],
              "report_valid": g.report_valid if g else None, "notes": g.notes if g else [],
              "timed_out": run.timed_out if run else None, "exit_code": run.exit_code if run else None,
              "is_error": run.is_error if run else None, "num_turns": run.num_turns if run else None,
              "cli_cost_usd_at_cli_prices": run.cli_cost_usd if run else None,
              "transcript_usage": run.extra.get("transcript_usage") if run else (u.model_dump() if u else None),
              "agent_result": run.result_text if run else "", "stderr_tail": run.stderr_tail if run else "",
              "artifacts": artifact_hashes(run_dir / "submission") if (run_dir / "submission").exists() else {},
              "tool_events": audit["counts"]["tool_events"], "egress": egress}
    review = [{"id": i.id, "evidence": i.evidence, "auto_earned": i.earned, "possible": i.possible}
              for i in (g.items if g else []) if i.review]
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    (run_dir / "detail.json").write_text(json.dumps(detail, indent=2, ensure_ascii=False, default=str))
    (run_dir / "review.json").write_text(json.dumps(review, indent=2, ensure_ascii=False))
    return {"result": result, "detail": detail, "run_dir": str(run_dir)}


def recover_runs(paths: Paths, store: ExperimentStore, adapter) -> list[dict[str, Any]]:
    """Finalise runs whose harness died: no result.json. Containers/networks removed, session recovered."""
    out = []
    for run_json in sorted((paths.root / "bench_runs").glob("*/run.json")):
        run_dir = run_json.parent
        if (run_dir / "result.json").exists():
            continue
        meta = json.loads(run_json.read_text())
        if "started_at" not in meta:
            continue  # prepared only (bench prepare), never run
        removed = cleanup_run(meta["run_id"])
        leftover = cleanup_run(meta["run_id"])
        recover_session(store, meta["session_id"], "harness process died; recovered by `zqr bench recover`")
        sess = store.get_session(meta["session_id"])
        ws = Path(sess["workspace"])
        sha = freeze_submission(ws, run_dir / "submission") if ws.exists() else None
        task = load_tasks()[meta["task_id"]]
        egress_log = run_dir / "egress.jsonl"
        records = [json.loads(x) for x in egress_log.read_text().splitlines() if x.strip()] \
            if egress_log.exists() else []
        faults = [*meta.get("faults", []), "harness_crash"]
        allow = [] if "model_unreachable" in meta.get("faults", []) else [model_endpoint()]
        repeat = re.search(r"-r(\d+)-", meta["run_id"])
        res = finalize(paths, store, run_dir, task, meta["run_id"], meta["session_id"],
                       repeat=int(repeat.group(1)) if repeat else 0, track="product", adapter=adapter, run=None,
                       g=None, faults=faults, egress_records=records, network=True, allow=allow,
                       mandate_id=hash_obj(sess["config"]["mandate"])[:16],
                       harness_error=f"harness process died; cleaned up {removed}", submission_sha=sha,
                       cny_per_usd=7.1, wall=time.time() - meta["started_at"], crash=True, leftover=leftover)
        out.append({"run_id": meta["run_id"], "status": res["result"]["status"],
                    "failure_class": res["result"]["failure_class"], "audit_ok": res["result"]["audit"]["ok"],
                    "removed": removed})
    return out


def screen(paths: Paths, store: ExperimentStore, tasks: list[str], adapter, *, max_cost_usd: float,
           cny_per_usd: float = 7.1, repeats: int = 1, faults: dict[str, list[str]] | None = None,
           title: str = "Benchmark v0.2 screen") -> dict[str, Any]:
    spent, runs = 0.0, []
    for r in range(1, repeats + 1):
        for task_id in tasks:
            if spent >= max_cost_usd:
                runs.append({"task_id": task_id, "skipped": f"budget reached ({spent:.4f} >= {max_cost_usd} USD)"})
                continue
            out = run_task(paths, store, task_id, adapter, repeat=r, cny_per_usd=cny_per_usd,
                           faults=(faults or {}).get(task_id))
            spent += out["result"]["cost_estimated_usd"] or 0.0
            runs.append(out)
    report = write_screen_report(paths, runs, spent, max_cost_usd, title)
    return {"spent_estimated_usd": round(spent, 6), "report": str(report), "runs": runs}


def write_screen_report(paths: Paths, runs: list[dict[str, Any]], spent: float, cap: float,
                        title: str = "Benchmark v0.2 screen") -> Path:
    lines = [f"# {title}", "",
             "Public development fixtures. Not a model ranking and not evidence of profitability. Judgement items "
             "carry automatic keyword/record evidence and are listed in each run's review.json for blind human "
             "review.", "", f"Estimated model cost: {spent:.4f} USD (cap {cap} USD; vendor bill not visible).", "",
             "| run | task | status | failure | score | audit | violations | faults | experiments | cost USD |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        if "skipped" in r:
            lines.append(f"| | {r['task_id']} | skipped | {r['skipped']} | | | | | | |")
            continue
        res = r["result"]
        nviol = {s: len(v) for s, v in res["violations"].items() if v}
        faults = ", ".join(f"{f['fault']}={f['outcome']}" for f in res["faults"]) or "-"
        score = f"{res['score']:.0%}" if res["score"] is not None else "-"
        lines.append(f"| {res['run_id']} | {res['task_id']} | **{res['status']}** | {res['failure_class']} | "
                     f"{score} | {'ok' if res['audit']['ok'] else 'FAIL'} | {nviol or 0} | {faults} | "
                     f"{res['experiments_submitted']} | {res['cost_estimated_usd']} |")
    for r in runs:
        if "skipped" in r:
            continue
        res, det = r["result"], r["detail"]
        lines += ["", f"## {res['task_id']} ({res['run_id']})", "",
                  f"- Session `{det['session_id']}`; config `{res['config_hash']}`; report valid: "
                  f"{det['report_valid']}; turns: {det['num_turns']}; tool events: {det['tool_events']}"]
        lines += [f"- Failure detail: {d}" for d in res["failure_details"][:5]]
        lines += [f"- Hard failure: {h}" for h in res["hard_failures"]]
        lines += [f"- Violation: {v}" for v in res["attempted_violations"]]
        lines += [f"- Fault {f['fault']}: **{f['outcome']}** ({f['evidence']})" for f in res["faults"]]
        lines += [f"- Audit problem: {p}" for p in res["audit"]["problems"]]
        if res["rubric_items"]:
            lines += ["", "| item | dimension | earned | possible | evidence |", "|---|---|---|---|---|"]
            lines += [f"| {i['id']} | {i['dimension']} | {i['earned']} | {i['possible']} | "
                      f"{i['evidence'].replace('|', '/')} |" for i in res["rubric_items"]]
        lines += [f"- Note: {n}" for n in det["notes"]]
    out = paths.root / "bench_runs" / f"screen-{time.strftime('%Y%m%d-%H%M%S')}.md"
    out.write_text("\n".join(lines) + "\n")
    return out
