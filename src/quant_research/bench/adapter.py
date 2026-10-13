"""Agent adapters. Claude Code headless (the Claude Agent SDK runtime) in the agent container, tools over MCP.

The container sees only the task workspace and a read-only MCP config with the session token. Model credentials
are passed by name (`-e NAME`), so values never appear on a command line; no exchange keys exist anywhere.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quant_research.controller.agent import TokenUsage, usage_from_output

AGENT_IMAGE = "quant-research-agent:latest"
MODEL_ENV = ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
             "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
             "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "API_TIMEOUT_MS"]
ALLOWED_TOOLS = "Read Write Edit Glob Grep mcp__quant"
DENIED_TOOLS = "Bash WebFetch WebSearch Task NotebookEdit"
PROMPT = ("You are running benchmark task research inside /workspace. Read /workspace/ROLE.md (your role) and "
          "/workspace/TASK.md (the task, budget, tools and deliverables), then complete the task using only the "
          "`quant` MCP tools for experiments. Write all deliverables under /workspace and validate research.json "
          "with the write_report tool before you finish.")


@dataclass
class AgentRun:
    adapter: str
    harness_version: str
    exit_code: int | None
    timed_out: bool
    wall_seconds: float
    usage: TokenUsage | None
    num_turns: int | None = None
    result_text: str = ""
    is_error: bool | None = None
    cli_cost_usd: float | None = None  # the CLI's own estimate at its built-in prices; not the vendor bill
    stderr_tail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class ClaudeContainerAdapter:
    name = "claude_code_container"

    def __init__(self, image: str = AGENT_IMAGE, model: str | None = None, memory: str = "2g", cpus: str = "2"):
        self.image, self.model, self.memory, self.cpus = image, model, memory, cpus
        self._version: str | None = None

    def harness_version(self) -> str:
        if self._version is None:
            out = subprocess.run(["docker", "run", "--rm", self.image, "claude", "--version"],
                                 capture_output=True, text=True, timeout=120)
            self._version = f"claude-code {out.stdout.strip().split()[0] if out.stdout.strip() else 'unknown'}"
        return self._version

    def command(self, name: str, workspace: Path, mcp_file: Path) -> list[str]:
        cmd = ["docker", "run", "--rm", "--name", name, "--init",
               "--add-host", "host.docker.internal:host-gateway",
               "--memory", self.memory, "--cpus", self.cpus, "--pids-limit", "512",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
               "--tmpfs", "/tmp:size=256m", "--tmpfs", "/home/node:size=256m,uid=1000,gid=1000",
               "-v", f"{workspace.resolve()}:/workspace", "-v", f"{mcp_file}:/etc/quant/mcp.json:ro"]
        for var in MODEL_ENV:
            cmd += ["-e", var]
        cmd += [self.image, "claude", "-p", PROMPT, "--output-format", "json",
                "--mcp-config", "/etc/quant/mcp.json", "--strict-mcp-config",
                "--permission-mode", "dontAsk", "--allowedTools", ALLOWED_TOOLS, "--disallowedTools", DENIED_TOOLS]
        return cmd + (["--model", self.model] if self.model else [])

    def run(self, workspace: Path, mcp_config: dict[str, Any], timeout_s: float) -> AgentRun:
        name = f"qr-agent-{uuid.uuid4().hex[:10]}"
        with tempfile.TemporaryDirectory(prefix="qr-mcp-") as tmp:
            mcp_file = Path(tmp) / "mcp.json"
            mcp_file.write_text(json.dumps(mcp_config))
            mcp_file.chmod(0o644)
            t0 = time.time()
            timed_out = False
            try:
                proc = subprocess.run(self.command(name, workspace, mcp_file), capture_output=True, text=True,
                                      timeout=timeout_s)
                stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
            except subprocess.TimeoutExpired as exc:
                subprocess.run(["docker", "kill", name], capture_output=True)
                timed_out, code = True, None
                stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            wall = time.time() - t0
        data: dict[str, Any] = {}
        try:
            parsed = json.loads(stdout.strip() or "{}")
            data = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            pass
        return AgentRun(adapter=self.name, harness_version=self.harness_version(), exit_code=code,
                        timed_out=timed_out, wall_seconds=wall, usage=usage_from_output(stdout),
                        num_turns=data.get("num_turns"), result_text=str(data.get("result") or "")[:4000],
                        is_error=data.get("is_error"), cli_cost_usd=data.get("total_cost_usd"),
                        stderr_tail=stderr[-2000:],
                        extra={"subtype": data.get("subtype"), "models": sorted(data.get("modelUsage") or {})})
