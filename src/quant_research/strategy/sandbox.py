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
import tempfile
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel

PACKAGE_SRC = Path(__file__).resolve().parents[2]


class SandboxConfig(BaseModel):
    isolation: Literal["subprocess", "docker"] = "subprocess"
    cpu_seconds: int = 120
    memory_mb: int = 4096
    wall_seconds: int = 300
    docker_image: str = "quant-research-sandbox:latest"


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


def _command(cfg: SandboxConfig, workdir: Path) -> tuple[list[str], dict[str, str]]:
    if cfg.isolation == "docker":
        if shutil.which("docker") is None:
            raise SandboxError("docker isolation requested but docker is not installed")
        cmd = [
            "docker", "run", "--rm", "--network", "none", "--read-only", "--pids-limit", "256",
            "--memory", f"{cfg.memory_mb}m", "--cpus", "1", "--tmpfs", "/tmp:size=256m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-v", f"{workdir}:/work", "-v", f"{PACKAGE_SRC}:/app/src:ro",
            "-e", "PYTHONPATH=/app/src", "-e", "PYTHONDONTWRITEBYTECODE=1",
            cfg.docker_image, "python", "-m", "quant_research.strategy.worker", "/work",
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
    with tempfile.TemporaryDirectory(prefix="qr-sandbox-") as tmp:
        workdir = Path(tmp)
        shutil.copyfile(code_path, workdir / "strategy.py")
        bars.to_parquet(workdir / "bars.parquet", index=False)
        (workdir / "request.json").write_text(json.dumps({"parameters": params,
                                                          "prefix_lengths": prefix_lengths or []}))
        cmd, env = _command(cfg, workdir)
        try:
            proc = subprocess.run(
                cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=cfg.wall_seconds,
                preexec_fn=_limits(cfg) if cfg.isolation == "subprocess" else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"strategy exceeded wall time of {cfg.wall_seconds}s") from exc
        result_path = workdir / "result.json"
        if not result_path.exists():
            raise SandboxError(f"worker exited {proc.returncode} without result: {proc.stderr[-2000:]}")
        result = json.loads(result_path.read_text())
        if not result["ok"]:
            raise SandboxError(result["error"])
        targets = pd.read_parquet(workdir / "targets.parquet")["target"].to_numpy(dtype=float)
        return targets, {int(k): v for k, v in result["prefix"].items()}, result.get("contract", "legacy_targets")
