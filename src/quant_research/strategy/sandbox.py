"""Execute candidate strategy code outside the research process.

`subprocess`: separate interpreter, CPU/memory/file rlimits, wall timeout, scrubbed environment,
temporary working directory, sockets disabled in-process. Not a security boundary.
`docker`: same worker in a container with --network none, read-only root, memory/CPU limits.
Agent-generated code must use `docker` unless weak isolation is explicitly allowed.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from quant_research.runtime import mount, runtime_image, scratch_dir

SANDBOX_USER = "10001:10001"


class SandboxConfig(BaseModel):
    isolation: Literal["subprocess", "docker"] = "subprocess"
    cpu_seconds: int = 120
    memory_mb: int = 4096
    wall_seconds: int = 300
    docker_image: str | None = None  # default: the release image, which carries the worker of this version
    run_label: str | None = Field(None, exclude=True)  # runtime only: lets a crashed harness's containers be found


class SandboxError(RuntimeError):
    pass


def _limits(cfg: SandboxConfig):
    def apply() -> None:
        import resource

        for name, value in (("RLIMIT_CPU", cfg.cpu_seconds), ("RLIMIT_AS", cfg.memory_mb * 1024 * 1024),
                            ("RLIMIT_FSIZE", 512 * 1024 * 1024), ("RLIMIT_CORE", 0)):
            # e.g. RLIMIT_AS is not enforceable on macOS
            with contextlib.suppress(ValueError, OSError, AttributeError):
                resource.setrlimit(getattr(resource, name), (value, value))

    return apply


def _command(cfg: SandboxConfig, workdir: Path, name: str) -> tuple[list[str], dict[str, str]]:
    if cfg.isolation == "docker":
        if shutil.which("docker") is None:
            raise SandboxError("docker isolation requested but docker is not installed")
        cmd = [
            "docker", "run", "--rm", "--name", name, "--label", "quant-research=sandbox",
            *(["--label", f"quant-research.run={cfg.run_label}"] if cfg.run_label else []),
            "--network", "none", "--read-only", "--pids-limit", "256",
            "--memory", f"{cfg.memory_mb}m", "--cpus", "1", "--tmpfs", "/tmp:size=256m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", SANDBOX_USER,
            *mount(workdir, "/work"), "-e", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python",
            cfg.docker_image or runtime_image(), "-m", "quant_research.strategy.worker", "/work",
        ]
        return cmd, {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    env = {"PATH": "/usr/bin:/bin", "HOME": str(workdir), "PYTHONHASHSEED": "0",
           "PYTHONDONTWRITEBYTECODE": "1", "MPLCONFIGDIR": str(workdir)}
    return [sys.executable, "-s", "-m", "quant_research.strategy.worker", str(workdir)], env


def run_plugin(
    code_path: Path,
    bars: pd.DataFrame,
    params: dict[str, Any],
    cfg: SandboxConfig,
    prefix_lengths: list[int] | None = None,
) -> tuple[np.ndarray, dict[int, float], str]:
    """Returns (values, prefix values, contract) where contract is `exposures` or `legacy_targets`."""
    with scratch_dir("qr-sandbox-") as workdir:
        shutil.copyfile(code_path, workdir / "strategy.py")
        bars.to_parquet(workdir / "bars.parquet", index=False)
        (workdir / "request.json").write_text(json.dumps({"parameters": params,
                                                          "prefix_lengths": prefix_lengths or []}))
        name = f"qr-sbx-{uuid.uuid4().hex[:12]}"
        cmd, env = _command(cfg, workdir, name)
        try:
            proc = subprocess.run(
                cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=cfg.wall_seconds,
                preexec_fn=_limits(cfg) if cfg.isolation == "subprocess" else None,
            )
        except subprocess.TimeoutExpired as exc:
            if cfg.isolation == "docker":  # killing the client leaves the container running
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
            raise SandboxError(f"strategy exceeded wall time of {cfg.wall_seconds}s") from exc
        result_path = workdir / "result.json"
        if not result_path.exists():
            raise SandboxError(f"worker exited {proc.returncode} without result: {proc.stderr[-2000:]}")
        result = json.loads(result_path.read_text())
        if not result["ok"]:
            raise SandboxError(result["error"])
        targets = pd.read_parquet(workdir / "targets.parquet")["target"].to_numpy(dtype=float)
        return targets, {int(k): v for k, v in result["prefix"].items()}, result.get("contract", "legacy_targets")
