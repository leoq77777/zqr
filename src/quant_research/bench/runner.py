"""Run benchmark tasks end to end: workspace -> gateway server -> agent container -> grade -> result.json."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from quant_research.bench.adapter import AgentRun, ClaudeContainerAdapter
from quant_research.bench.grader import Grade, artifact_hashes, grade
from quant_research.bench.tasks import load_tasks
from quant_research.bench.workspace import prepare
from quant_research.controller.budget import ModelPricing, pricing_preset
from quant_research.controller.controller import SessionState
from quant_research.experiments.store import ExperimentStore
from quant_research.gateway.mcp_server import GatewayHTTPServer
from quant_research.hashing import hash_obj
from quant_research.paths import Paths
from quant_research.strategy.sandbox import SandboxConfig

PASS_SCORE = 0.6


def _pricing(models: list[str], cny_per_usd: float) -> ModelPricing | None:
    joined = ",".join(models).lower()
    if "deepseek-v4-pro" in joined or "deepseek-pro" in joined:
        return pricing_preset("deepseek-v4-pro", cny_per_usd)
    if "deepseek" in joined:
        return pricing_preset("deepseek-flash", cny_per_usd)
    return None


def _status(run: AgentRun, g: Grade, experiments: int, tool_events: int) -> str:
    if (run.exit_code not in (0, None) or run.is_error) and tool_events == 0 and run.usage is None:
        return "invalid"  # the environment failed before the agent could work
    if g.report_valid is None or (g.report_valid is False and not g.items):
        return "incomplete"
    if g.hard_failures or g.score() < PASS_SCORE:
        return "failed"
    return "passed"


def run_task(paths: Paths, store: ExperimentStore, task_id: str, adapter: ClaudeContainerAdapter, *,
             repeat: int = 1, track: str = "product", cny_per_usd: float = 7.1,
             sandbox: SandboxConfig | None = None, allow_weak_isolation: bool = False) -> dict[str, Any]:
    task = load_tasks()[task_id]
    run_id = f"{task_id.lower()}-r{repeat}-{uuid.uuid4().hex[:8]}"
    prep = prepare(paths, store, task, run_id, sandbox, allow_weak_isolation)
    store.update_session(prep.session_id, state=SessionState.RUNNING)
    with GatewayHTTPServer(paths, store, prep.session_id, faults=task.faults) as srv:
        run = adapter.run(prep.workspace, srv.client_config(), timeout_s=task.wall_seconds)
    store.update_session(prep.session_id, state=SessionState.COMPLETED,
                         stop_reason="benchmark run finished" if not run.timed_out else "wall clock exceeded")
    g = grade(paths, store, task_id, prep.session_id, sandbox)

    exps = store.list_experiments(limit=1000, session_id=prep.session_id)
    events = store.session_events(prep.session_id)
    tool_events = sum(e["event"].startswith("tool_") for e in events)
    pricing = _pricing(run.extra.get("models") or [], cny_per_usd)
    u = run.usage
    cost = pricing.cost(u.input_tokens, u.output_tokens, u.cache_read_tokens, ts=time.time()) if (u and pricing) \
        else None
    unavailable = ["cost_billed_usd: vendor bill not visible to the harness",
                   "model_requests: the CLI reports agent turns, not HTTP requests"]
    if cost is None:
        unavailable.append("cost_estimated_usd: token usage or vendor pricing unavailable")
    config = {"adapter": adapter.name, "image": adapter.image, "model_flag": adapter.model,
              "task": task_id, "budget": task.budget, "faults": task.faults}
    result = {
        "run_id": run_id, "task_id": task_id, "repeat": repeat, "track": track, "adapter": adapter.name,
        "harness_version": run.harness_version, "model_id": ",".join(run.extra.get("models") or []) or "unknown",
        "provider": "deepseek (Anthropic-compatible endpoint)" if pricing else "unknown",
        "config_hash": hash_obj(config)[:16], "status": _status(run, g, len(exps), tool_events),
        "cost_estimated_usd": round(cost, 6) if cost is not None else None, "cost_billed_usd": None,
        "wall_seconds": round(run.wall_seconds, 2),
        "backtest_seconds": round(sum((e["finished_at"] or e["created_at"]) - e["created_at"] for e in exps), 3),
        "model_requests": None,
        "experiments_submitted": sum(e["status"] != "blocked" for e in exps),
        "hard_failures": g.hard_failures, "attempted_violations": g.attempted_violations,
        "rubric_items": [i.out() for i in g.items], "unavailable_fields": unavailable,
        "report_path": str(prep.workspace / "report.md") if (prep.workspace / "report.md").exists() else None,
    }
    detail = {"session_id": prep.session_id, "workspace": str(prep.workspace), "score": round(g.score(), 4),
              "report_valid": g.report_valid, "notes": g.notes, "timed_out": run.timed_out,
              "exit_code": run.exit_code, "is_error": run.is_error, "num_turns": run.num_turns,
              "usage": u.model_dump() if u else None, "cli_cost_usd_at_cli_prices": run.cli_cost_usd,
              "agent_result": run.result_text, "stderr_tail": run.stderr_tail,
              "artifacts": artifact_hashes(prep.workspace), "tool_events": tool_events}
    review = [{"id": i.id, "evidence": i.evidence, "auto_earned": i.earned, "possible": i.possible}
              for i in g.items if i.review]
    (prep.run_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    (prep.run_dir / "detail.json").write_text(json.dumps(detail, indent=2, ensure_ascii=False, default=str))
    (prep.run_dir / "review.json").write_text(json.dumps(review, indent=2, ensure_ascii=False))
    return {"result": result, "detail": detail, "run_dir": str(prep.run_dir)}


def screen(paths: Paths, store: ExperimentStore, tasks: list[str], adapter: ClaudeContainerAdapter, *,
           max_cost_usd: float, cny_per_usd: float = 7.1) -> dict[str, Any]:
    spent, runs = 0.0, []
    for task_id in tasks:
        if spent >= max_cost_usd:
            runs.append({"task_id": task_id, "skipped": f"budget reached ({spent:.4f} >= {max_cost_usd} USD)"})
            continue
        out = run_task(paths, store, task_id, adapter, cny_per_usd=cny_per_usd)
        spent += out["result"]["cost_estimated_usd"] or 0.0
        runs.append(out)
    report = write_screen_report(paths, runs, spent, max_cost_usd)
    return {"spent_estimated_usd": round(spent, 6), "report": str(report), "runs": runs}


DIMENSIONS = {"correctness": 30, "method": 25, "reproducibility": 20, "evidence": 15, "recovery": 10}


def write_screen_report(paths: Paths, runs: list[dict[str, Any]], spent: float, cap: float) -> Path:
    lines = ["# Benchmark v0.2 initial screen", "",
             "Feasibility screen, one run per task, public development fixtures. Not a model ranking and not "
             "evidence of profitability. Judgement items carry automatic keyword/record evidence and are listed "
             "in each run's review.json for blind human review.", "",
             f"Estimated model cost: {spent:.4f} USD (cap {cap} USD; vendor bill not visible).", "",
             "| task | status | hard failures | attempted violations | score | experiments | wall s | cost USD | "
             "model |", "|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        if "skipped" in r:
            lines.append(f"| {r['task_id']} | skipped | | | | | | | {r['skipped']} |")
            continue
        res, det = r["result"], r["detail"]
        lines.append(f"| {res['task_id']} | **{res['status']}** | {len(res['hard_failures'])} | "
                     f"{len(res['attempted_violations'])} | {det['score']:.0%} | {res['experiments_submitted']} | "
                     f"{res['wall_seconds']:.0f} | {res['cost_estimated_usd']} | {res['model_id']} |")
    for r in runs:
        if "skipped" in r:
            continue
        res, det = r["result"], r["detail"]
        lines += ["", f"## {res['task_id']} ({res['run_id']})", "",
                  f"- Session `{det['session_id']}`; report valid: {det['report_valid']}; turns: {det['num_turns']}; "
                  f"timed out: {det['timed_out']}; tool events: {det['tool_events']}"]
        lines += [f"- Hard failure: {h}" for h in res["hard_failures"]]
        lines += [f"- Attempted violation: {v}" for v in res["attempted_violations"]]
        lines += ["", "| item | dimension | earned | possible | evidence |", "|---|---|---|---|---|"]
        lines += [f"| {i['id']} | {i['dimension']} | {i['earned']} | {i['possible']} | "
                  f"{i['evidence'].replace('|', '/')} |" for i in res["rubric_items"]]
        lines += [f"- Note: {n}" for n in det["notes"]]
    out = paths.root / "bench_runs" / f"screen-{time.strftime('%Y%m%d-%H%M%S')}.md"
    out.write_text("\n".join(lines) + "\n")
    return out
