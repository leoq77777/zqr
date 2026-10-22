"""Per-run task workspace and session: only the role prompt, the task brief, allowed fixtures and the schema."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from quant_research.bench.fixtures import ingest
from quant_research.bench.tasks import CONTRACT_V02, BenchTask, benchmark_dir
from quant_research.controller.agent import ManualRunner
from quant_research.controller.budget import Budget
from quant_research.controller.controller import ResearchController, SessionConfig
from quant_research.experiments.store import ExperimentStore
from quant_research.paths import Paths
from quant_research.research.mandate import Mandate
from quant_research.strategy.sandbox import SandboxConfig

PROTECTED = ("evaluator_private", "rubric.md", "tasks.json", "README.md", "v0.2/README.md")


@dataclass
class PreparedRun:
    run_id: str
    task: BenchTask
    session_id: str
    workspace: Path
    run_dir: Path


def _mandate(task: BenchTask, dataset_id: str, split_id: str) -> Mandate:
    b = task.budget
    return Mandate.model_validate({
        "question": f"{task.id} {task.title}: {task.brief}", "dataset_id": dataset_id, "split_id": split_id,
        "engine_config": {}, "policy": {}, "evaluation_design": {"kind": "single_validation"},
        "primary_metric": "sharpe",
        "acceptance": {"min_primary": 0.5, "min_dsr": None, "min_round_trips": 4, "max_drawdown": -0.5,
                       "dsr_waiver": "benchmark run: the evaluator grades the research, no candidate is accepted",
                       "require_costs_x2": True, "require_delay": True},
        "budget": Budget(max_rounds=1, max_candidates=1, max_experiments=int(b.get("max_experiments", 0)),
                         max_wall_seconds=float(b.get("max_wall_seconds", 900)),
                         max_model_cost_usd=b.get("max_model_cost_usd"), max_trials_per_strategy=None),
        "holdout": {"max_viewings": 1, "min_primary": 0.0, "min_total_return": 0.0},
        "agent_segments": list(task.agent_segments),
        "notes": f"benchmark v0.2 task {task.id}",
    })


def task_brief(task: BenchTask, run_id: str, segments: tuple[str, ...]) -> str:
    b = task.budget
    fixtures = "\n".join(f"- `/workspace/fixtures/{f}`" for f in task.visible_fixtures)
    return f"""# Task {task.id}: {task.title}

research_id: `{run_id}`  task_id: `{task.id}`

## Brief

{task.brief}

## Inputs

{fixtures}
- Role prompt: `/workspace/CLAUDE.md` (Claude Code project instructions).
- Output schema: `/workspace/research.schema.json`.

## Budget (enforced by the controller)

- Experiments: {b.get("max_experiments", 0)} (blocked and duplicate requests are recorded; budget is not reset)
- Wall clock: {int(b.get("max_wall_seconds", 900))} s; model cost: {b.get("max_model_cost_usd")} USD
- Backtest concurrency: {b.get("backtest_concurrency", 1)}

## Tools (MCP server `quant`)

- `inspect_dataset` -> dataset identity, schema, allowed segments ({", ".join(segments)}), costs and policy.
- `submit_plan(plan)` -> plan_id. Preregister hypothesis, mechanism, falsification, artifact, parameter_ranges,
  planned_experiments before running experiments.
- `submit_experiment(artifact, idempotency_key, parameters, plan_id, segment)` -> experiment record. `artifact` is
  a `.py` file under `/workspace/` implementing `/workspace/fixtures/strategy_contract.md` (or a built-in template
  name). Use a new idempotency key per intended experiment and the same key when retrying after a timeout.
- `get_experiment(experiment_id)`, `compare_experiments(experiment_ids)`, `history()`.
- `write_report("research.json")` validates your research.json (experiment ids, artifact hashes, cited numbers).

There is no shell and no network besides these tools. Files you may write: anything under `/workspace/`.

## Deliverables

- `/workspace/research.json` following the schema; list every experiment id you ran. Put cited numbers under an
  optional `results: [{{"experiment_id": ..., "metrics": {{...}}}}]` list so they can be verified.
- `/workspace/report.md`: what you did, what the evidence supports and does not, all attempts, next step.
- Strategy code files under `/workspace/`, listed in `strategy_artifacts` with their sha256.
- `final_artifact` in research.json: the one strategy you submit as your result, as
  `{{"path", "sha256", "parameters", "experiment_id"}}`, where the experiment is a completed run of exactly that
  file with exactly those parameters (`null` if you recommend no strategy). Only this declared artifact is graded;
  other files and runs are not considered.
Call `write_report` before finishing and fix any problem it reports. The workspace is frozen when you finish.
"""


def prepare(paths: Paths, store: ExperimentStore, task: BenchTask, run_id: str,
            sandbox: SandboxConfig | None = None, allow_weak_isolation: bool = False) -> PreparedRun:
    if task.dataset is None:
        raise ValueError(f"{task.id} has no experiment dataset in v0.2; it is review-only")
    root = benchmark_dir()
    snap, split = ingest(paths, task.dataset, root)
    cfg = SessionConfig(mandate=_mandate(task, snap["dataset_id"], split.split_id),
                        sandbox=sandbox or SandboxConfig(isolation="docker"),
                        allow_weak_isolation=allow_weak_isolation)
    sid = ResearchController(paths, store, ManualRunner()).create_session(cfg)
    ws = Path(store.get_session(sid)["workspace"])
    (ws / "fixtures").mkdir()
    shutil.copyfile(root / "role_prompt.md", ws / "CLAUDE.md")
    shutil.copyfile(root / "v0.2" / "research.schema.json", ws / "research.schema.json")
    for name in task.visible_fixtures:
        src = root / CONTRACT_V02 if name == "strategy_contract.md" else root / "fixtures" / name
        shutil.copyfile(src, ws / "fixtures" / name)
    (ws / "TASK.md").write_text(task_brief(task, run_id, task.agent_segments))
    run_dir = paths.root / "bench_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({"run_id": run_id, "task_id": task.id, "session_id": sid,
                                                   "dataset_id": snap["dataset_id"], "split_id": split.split_id},
                                                  indent=2))
    leaked = [p for p in PROTECTED if (ws / p).exists() or (ws / "fixtures" / Path(p).name).exists()]
    if leaked:
        raise RuntimeError(f"protected benchmark files in the agent workspace: {leaked}")
    return PreparedRun(run_id, task, sid, ws, run_dir)
