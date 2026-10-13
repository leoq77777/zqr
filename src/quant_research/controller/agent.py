"""AgentRunner adapters. The controller never assumes a specific coding-agent product interface."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from quant_research.strategy.templates import TEMPLATES

SUBMISSION_FILE = "submission.json"
SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSPHRASE|CREDENTIAL|PRIVATE)", re.I)


class CandidateSubmission(BaseModel):
    template: str | None = None
    plugin: str | None = None  # path relative to the round's candidate directory
    parameters: dict[str, Any] = Field(default_factory=dict)
    hypothesis: str = ""
    plan_id: str | None = None  # preregistered plan (tool `plan`); without one a retrospective plan is recorded
    parameter_grid: dict[str, list[Any]] | None = None  # selection grid for walk-forward mandates


class TokenUsage(BaseModel):
    model: str | None = None
    input_tokens: int = 0  # cache misses and cache writes
    cache_read_tokens: int = 0  # cache hits, usually much cheaper
    output_tokens: int = 0


class AgentRoundResult(BaseModel):
    candidates: list[CandidateSubmission] = Field(default_factory=list)
    done: bool = False
    report: str = ""
    usage: TokenUsage | None = None
    raw_output: str = ""


class ResearchTask(BaseModel):
    session_id: str
    round: int
    question: str
    dataset_id: str
    split_id: str
    research_data_path: str
    candidate_dir: str
    allowed_segments: list[str] = ["dev"]
    remaining: dict[str, Any]
    history: list[dict[str, Any]]
    feedback: list[dict[str, Any]] = Field(default_factory=list)
    acceptance: dict[str, Any]
    quant_home: str
    trials_on_split: int = 0

    def prompt(self) -> str:
        templates = "\n".join(f"- `{t.name}`: {t.description}; params {list(t.params_model.model_fields)}"
                              for t in TEMPLATES.values())
        history = json.dumps(self.history[-30:], indent=1, default=str) if self.history else "[]"
        feedback = "\n".join(f"- `{f['label']}` x{f['count']}: {f['suggestion']}"
                             for f in self.feedback) or "- none yet"
        return f"""# Research task (session {self.session_id}, round {self.round})

Question: {self.question}

You are a quantitative researcher. Propose long/flat spot strategies for dataset `{self.dataset_id}`.
Rules:
- Only data you may read: `{self.research_data_path}` (dev segment only). Validation and final holdout data
  are not available to you and must not be sought.
- Work only inside `{self.candidate_dir}`. You have no trading keys and must not place orders.
- Strategy contract: either a built-in template, or a Python file defining
  `generate_exposures(history: pandas.DataFrame, parameters: dict) -> sequence of floats in [0, 1]`: the exposure
  proposal decided at row t's close, using only rows <= t (return 0 during warmup, never NaN). The controller's
  portfolio policy turns proposals into weights and the engine fills at the next bar's open; lookahead is
  detected and rejected. Results are compared with holding the asset at the same average exposure.
- Templates:
{templates}
- Tools (each experiment counts against the budget and is recorded, including failures):
  `quant tool plan --session {self.session_id} --file plan.json` -> plan_id. Preregister before experimenting:
    {{"hypothesis", "mechanism", "falsification", "artifact": template name or plugin file,
     "parameter_ranges": {{"lookback": {{"values": [12, 24]}}}}, "planned_experiments": 4}}
  `quant tool submit --session {self.session_id} --artifact <template or file.py> --params '{{...}}'
    --plan <plan_id> --key <unique idempotency key>` (retry with the same key never runs twice)
  `quant tool get --session {self.session_id} <experiment_id>`
  `quant tool compare --session {self.session_id} <id1> <id2> ...` (same segment, data, costs and policy only)
  `quant tool inspect --session {self.session_id}` (dataset schema, quality, dev window, costs)
  `quant tool history --session {self.session_id}`
  Experiments outside a plan are flagged unplanned/out_of_plan in every report.
- Exploration is limited to the dev segment. Validation is reserved for the controller: it evaluates every
  candidate you submit under the research mandate and reports the results to you next round.
- Mandate evaluation design and acceptance criteria: {json.dumps(self.acceptance)}
- Every trial counts: {self.trials_on_split} research trials already exist on this split, and the Deflated Sharpe
  threshold gets stricter as trials grow. Identical experiments are reused, not re-run, but still count.
- Remaining budget: {json.dumps(self.remaining)}
- Concluding that no strategy is worth pursuing is an acceptable outcome; set "done": true.

Failure patterns diagnosed so far (address these rather than re-tuning parameters):
{feedback}

Previous candidates (controller-computed metrics and diagnosis labels):
{history}

Finish by writing `{self.candidate_dir}/{SUBMISSION_FILE}`:
{{"candidates": [{{"template" or "plugin": ..., "parameters": {{...}}, "hypothesis": "...", "plan_id": "..."}}],
  "done": false, "report": "markdown notes"}}
Token usage is measured by the controller; do not report it yourself.
"""


class AgentRunner(Protocol):
    def run_round(self, task: ResearchTask) -> AgentRoundResult | None:
        """Return None if the round is not finished yet (e.g. waiting for a human-driven agent)."""


def _read_submission(candidate_dir: Path, raw_output: str = "") -> AgentRoundResult | None:
    path = candidate_dir / SUBMISSION_FILE
    if not path.exists():
        return None
    result = AgentRoundResult.model_validate_json(path.read_text())
    result.raw_output = raw_output or result.raw_output
    return result


def _write_task(task: ResearchTask) -> Path:
    d = Path(task.candidate_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.json").write_text(task.model_dump_json(indent=2))
    (d / "task.md").write_text(task.prompt())
    return d


class ManualRunner:
    """Human starts any coding agent on task.md; the controller resumes once submission.json exists."""

    def run_round(self, task: ResearchTask) -> AgentRoundResult | None:
        d = _write_task(task)
        return _read_submission(d)


def usage_from_output(stdout: str) -> TokenUsage | None:
    """Token usage from a JSON result printed by the agent CLI (Claude Code `--output-format json`)."""
    try:
        data = json.loads(stdout.strip())
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    per_model = data.get("modelUsage")
    if isinstance(per_model, dict) and per_model:
        rows = [m for m in per_model.values() if isinstance(m, dict)]
        return TokenUsage(
            model=",".join(per_model),
            input_tokens=sum(int(m.get(k) or 0) for m in rows for k in ("inputTokens", "cacheCreationInputTokens")),
            cache_read_tokens=sum(int(m.get("cacheReadInputTokens") or 0) for m in rows),
            output_tokens=sum(int(m.get("outputTokens") or 0) for m in rows))
    usage = data.get("usage")
    if isinstance(usage, dict):
        return TokenUsage(
            input_tokens=sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens")),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0))
    return None


def usage_from_transcripts(root: Path) -> TokenUsage | None:
    """Token usage summed from Claude Code session transcripts (`~/.claude/projects/**/*.jsonl`).

    Used when the CLI never printed its final result (killed agent). Streaming writes one line per content block
    with the same message id and usage, so usage is counted once per message id.
    """
    per_message: dict[str, tuple[str | None, dict[str, Any]]] = {}
    for path in sorted(root.rglob("*.jsonl")) if root.exists() else []:
        for line in path.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            msg = row.get("message") if isinstance(row, dict) and row.get("type") == "assistant" else None
            if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                per_message[str(msg.get("id") or row.get("uuid"))] = (msg.get("model"), msg["usage"])
    if not per_message:
        return None
    rows = [u for _, u in per_message.values()]
    return TokenUsage(
        model=",".join(sorted({m for m, _ in per_message.values() if m})) or None,
        input_tokens=sum(int(u.get(k) or 0) for u in rows for k in ("input_tokens", "cache_creation_input_tokens")),
        cache_read_tokens=sum(int(u.get("cache_read_input_tokens") or 0) for u in rows),
        output_tokens=sum(int(u.get("output_tokens") or 0) for u in rows))


CLAUDE_PROMPT = "Read {task_file} and follow it exactly. Write submission.json into {workdir}."
CLAUDE_TOOLS = "Read Write Edit Glob Grep Bash(quant tool *)"
CLAUDE_PASS_ENV = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]


def claude_command(model: str | None = None) -> list[str]:
    """Claude Code in non-interactive mode, restricted to file edits in the round directory and quant tools."""
    cmd = ["claude", "-p", CLAUDE_PROMPT, "--permission-mode", "acceptEdits", "--allowedTools", CLAUDE_TOOLS,
           "--output-format", "json"]
    return cmd + (["--model", model] if model else [])


class CommandRunner:
    """Run a configured agent CLI per round. Placeholders: {task_file} {workdir} {session}."""

    def __init__(self, command: list[str], timeout_s: float = 3600, pass_env: list[str] | None = None):
        self.command = command
        self.timeout_s = timeout_s
        self.pass_env = set(pass_env or [])

    def _env(self, task: ResearchTask) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in self.pass_env or not SECRET_ENV.search(k)}
        # The agent calls `quant tool ...`; make the running interpreter's scripts resolvable.
        env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
        env.update(QUANT_SESSION=task.session_id, QUANT_HOME=task.quant_home)
        return env

    def run_round(self, task: ResearchTask) -> AgentRoundResult | None:
        d = _write_task(task)
        values = {"task_file": str(d / "task.md"), "workdir": str(d), "session": task.session_id}
        cmd = []
        for part in self.command:
            for key, value in values.items():
                part = part.replace("{" + key + "}", value)
            cmd.append(part)
        t0 = time.time()
        measured = None
        try:
            proc = subprocess.run(cmd, cwd=d, env=self._env(task), stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=self.timeout_s)
            measured = usage_from_output(proc.stdout)
            output = proc.stdout[-20000:] + ("\n[stderr]\n" + proc.stderr[-5000:] if proc.stderr else "")
        except subprocess.TimeoutExpired:
            output = f"agent timed out after {self.timeout_s}s"
        (d / "agent_output.log").write_text(f"elapsed {time.time() - t0:.1f}s\n{output}")
        result = _read_submission(d, output) or AgentRoundResult(report=f"agent produced no {SUBMISSION_FILE}",
                                                                 raw_output=output)
        if measured is not None:  # measured usage beats self-reported usage
            result.usage = measured
        return result


class ScriptedRunner:
    """Deterministic runner for tests and dry runs."""

    def __init__(self, fn: Callable[[ResearchTask], AgentRoundResult | None]):
        self.fn = fn

    def run_round(self, task: ResearchTask) -> AgentRoundResult | None:
        _write_task(task)
        return self.fn(task)
