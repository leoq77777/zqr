"""Cross-run ledger (cost, violations, failures) and repeat analysis (variance, explanations, determinism)."""

from __future__ import annotations

import csv
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from quant_research.bench.grader import VIOLATION_SOURCES, grade
from quant_research.experiments.store import ExperimentStore
from quant_research.paths import Paths
from quant_research.strategy.sandbox import SandboxConfig


def load_results(paths: Paths, run_ids: list[str] | None = None) -> list[dict[str, Any]]:
    out = []
    for f in sorted((paths.root / "bench_runs").glob("*/result.json")):
        res = json.loads(f.read_text())
        if res.get("schema_version") != "v0.2" or (run_ids and res["run_id"] not in run_ids):
            continue
        res["_dir"] = str(f.parent)
        res["_mtime"] = f.stat().st_mtime
        out.append(res)
    return sorted(out, key=lambda r: r["_mtime"])


def ledger_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for r in results:
        u = r.get("usage") or {}
        rows.append({
            "run_id": r["run_id"], "task": r["task_id"], "repeat": r["repeat"], "status": r["status"],
            "failure_class": r["failure_class"], "score": r["score"], "cost_usd": r["cost_estimated_usd"],
            "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
            "cache_read_tokens": u.get("cache_read_tokens"), "usage_source": r.get("usage_source"),
            "experiments": r["experiments_submitted"],
            **{f"viol_{s}": len(r["violations"][s]) for s in VIOLATION_SOURCES},
            "hard_failures": len(r["hard_failures"]), "audit_ok": r["audit"]["ok"],
            "faults": ";".join(f"{f['fault']}={f['outcome']}" for f in r["faults"]),
            "network": r["network_isolation"], "config_hash": r["config_hash"],
            "submission_sha256": (r["submission_sha256"] or "")[:16], "wall_s": r["wall_seconds"],
            "failure_detail": (r["failure_details"] or [""])[0][:160],
        })
    return rows


def write_ledger(paths: Paths, results: list[dict[str, Any]], out_dir: Path | None = None) -> dict[str, Any]:
    rows = ledger_rows(results)
    out_dir = out_dir or paths.root / "bench_runs"
    csv_path = out_dir / "ledger.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["run_id"])
        w.writeheader()
        w.writerows(rows)
    total_cost = sum(r["cost_usd"] or 0 for r in rows)
    by_status, by_failure = Counter(r["status"] for r in rows), Counter(r["failure_class"] for r in rows)
    viol = {s: sum(r[f"viol_{s}"] for r in rows) for s in VIOLATION_SOURCES}
    faults = Counter(f.split("=")[1] for r in rows for f in r["faults"].split(";") if f)
    unpriced = [r["run_id"] for r in rows if r["cost_usd"] is None]
    lines = ["# Benchmark ledger", "", f"Runs: {len(rows)}; estimated model cost: {total_cost:.4f} USD"
             + (f" (unpriced runs: {unpriced})" if unpriced else ""), "",
             f"- Status: {dict(by_status)}", f"- Failure classes: {dict(by_failure)}",
             f"- Violations by source: {viol}", f"- Fault outcomes: {dict(faults)}",
             f"- Audit failures: {[r['run_id'] for r in rows if not r['audit_ok']]}", "",
             "| run | task | status | failure | score | cost USD | tokens in/out | violations | audit | faults | "
             "config |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        v = {s: r[f"viol_{s}"] for s in VIOLATION_SOURCES if r[f"viol_{s}"]}
        lines.append(f"| {r['run_id']} | {r['task']} | {r['status']} | {r['failure_class']} | {r['score']} | "
                     f"{r['cost_usd']} | {r['input_tokens']}/{r['output_tokens']}"
                     f"{' (transcript)' if r['usage_source'] == 'transcript' else ''} | {v or 0} | "
                     f"{'ok' if r['audit_ok'] else 'FAIL'} | {r['faults'] or '-'} | {r['config_hash']} |")
    md_path = out_dir / "ledger.md"
    md_path.write_text("\n".join(lines) + "\n")
    return {"runs": len(rows), "cost_usd": round(total_cost, 6), "status": dict(by_status),
            "failure_classes": dict(by_failure), "violations": viol, "fault_outcomes": dict(faults),
            "csv": str(csv_path), "markdown": str(md_path)}


def regrade(paths: Paths, store: ExperimentStore, result: dict[str, Any],
            sandbox: SandboxConfig | None = None) -> dict[str, Any]:
    """Grade the frozen submission again; a trustworthy grader returns exactly the stored items."""
    run_dir = Path(result["_dir"])
    sid = json.loads((run_dir / "run.json").read_text())["session_id"]
    g = grade(paths, store, result["task_id"], sid, sandbox, submission=run_dir / "submission")
    again = [i.out() for i in g.items]
    diffs = [(a, b) for a, b in zip(result["rubric_items"], again, strict=False) if a != b]
    if len(again) != len(result["rubric_items"]):
        diffs.append(("item count", (len(result["rubric_items"]), len(again))))
    if sorted(g.hard_failures) != sorted(result["hard_failures"]):
        diffs.append(("hard_failures", (result["hard_failures"], g.hard_failures)))
    return {"run_id": result["run_id"], "identical": not diffs, "differences": diffs}


def repeat_report(paths: Paths, store: ExperimentStore, results: list[dict[str, Any]], *,
                  sandbox: SandboxConfig | None = None, check_regrade: bool = True,
                  title: str = "Repeat analysis") -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        if not r["faults"]:
            groups[(r["task_id"], r["config_hash"])].append(r)
    regrades = {r["run_id"]: regrade(paths, store, r, sandbox) for r in results if r["rubric_items"]} \
        if check_regrade else {}
    lines = [f"# {title}", "", "Runs grouped by task and configuration hash (same image digest, harness, model flag, "
             "task, mandate, fixtures, grader version, egress allowlist). Fault runs are reported separately.", ""]
    summary = []
    for (task, cfg), runs in sorted(groups.items()):
        scores = [r["score"] for r in runs if r["score"] is not None]
        passed = sum(r["status"] == "passed" for r in runs)
        costs = [r["cost_estimated_usd"] or 0 for r in runs]
        finals = Counter(r["submission_sha256"] for r in runs)
        row = {"task": task, "config_hash": cfg, "runs": len(runs), "passed": passed,
               "score_mean": round(statistics.mean(scores), 4) if scores else None,
               "score_min": min(scores, default=None), "score_max": max(scores, default=None),
               "score_sd": round(statistics.stdev(scores), 4) if len(scores) > 1 else 0.0,
               "cost_mean": round(statistics.mean(costs), 6), "failure_classes": dict(Counter(r["failure_class"]
                                                                                             for r in runs)),
               "regrade_identical": all(regrades[r["run_id"]]["identical"] for r in runs if r["run_id"] in regrades)}
        summary.append(row)
        lines += [f"## {task} (config `{cfg}`)", "",
                  f"- Runs: {len(runs)}; passed: {passed}/{len(runs)}; score mean {row['score_mean']}, "
                  f"range [{row['score_min']}, {row['score_max']}], sd {row['score_sd']}; mean cost "
                  f"{row['cost_mean']} USD", f"- Failure classes: {row['failure_classes']}",
                  f"- Distinct submissions: {len(finals)} of {len(runs)} runs",
                  f"- Regrade of frozen submissions identical to the recorded grade: {row['regrade_identical']}"]
        items: dict[str, list[tuple[str, float, str]]] = defaultdict(list)
        for r in runs:
            for i in r["rubric_items"]:
                items[i["id"]].append((r["run_id"], i["earned"], i["evidence"]))
        varying = {k: v for k, v in items.items() if len({e for _, e, _ in v}) > 1}
        if varying:
            lines += ["", "Items that differ between runs (evidence per run explains the difference):", ""]
            for k, v in varying.items():
                lines.append(f"- **{k}**")
                lines += [f"  - {rid}: {earned} — {ev[:220]}" for rid, earned, ev in v]
        else:
            lines.append("- Every rubric item earned the same score in every run.")
        lines.append("")
    fault_runs = [r for r in results if r["faults"]]
    if fault_runs:
        lines += ["## Fault runs", "", "| run | task | faults | status | failure | audit |",
                  "|---|---|---|---|---|---|"]
        for r in fault_runs:
            f = "; ".join(f"{x['fault']}: **{x['outcome']}** ({x['evidence'][:120]})" for x in r["faults"])
            lines.append(f"| {r['run_id']} | {r['task_id']} | {f} | {r['status']} | {r['failure_class']} | "
                         f"{'ok' if r['audit']['ok'] else 'FAIL'} |")
    bad = [x for x in regrades.values() if not x["identical"]]
    if bad:
        lines += ["", "## Regrade differences", ""] + [f"- {x['run_id']}: {x['differences']}" for x in bad]
    out = paths.root / "bench_runs" / f"repeat-{time.strftime('%Y%m%d-%H%M%S')}.md"
    out.write_text("\n".join(lines) + "\n")
    return {"report": str(out), "groups": summary, "regrade_failures": [x["run_id"] for x in bad]}
