"""Frozen, content-addressed Python strategy plugins.

A plugin file must define `generate_exposures(history: pd.DataFrame, parameters: dict) -> sequence[float]`
(contract v0.2), or one of the legacy final-target entry points `target_positions` / `generate_targets`.
"""

from __future__ import annotations

import ast
import json
import platform
import shutil
import time
from importlib import metadata
from pathlib import Path

from quant_research.hashing import sha256_file
from quant_research.paths import Paths
from quant_research.runtime import lockfile
from quant_research.strategy.spec import StrategyError

ENTRYPOINTS = ("generate_exposures", "target_positions", "generate_targets")


def environment_lock() -> dict[str, str | None]:
    lock = lockfile()
    versions = {}
    for pkg in ("numpy", "pandas", "pyarrow"):
        try:
            versions[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            versions[pkg] = None
    return {"python": platform.python_version(), "uv_lock_sha256": sha256_file(lock) if lock.exists() else None,
            **versions}


def check_plugin_source(source: Path) -> None:
    tree = ast.parse(source.read_text(), filename=str(source))
    if not any(isinstance(n, ast.FunctionDef) and n.name in ENTRYPOINTS for n in tree.body):
        raise StrategyError(f"{source} must define top-level function generate_exposures(history, parameters)")


def register_plugin(paths: Paths, source: Path) -> str:
    source = Path(source)
    check_plugin_source(source)
    sha = sha256_file(source)
    dest_dir = paths.artifacts / "strategies"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{sha}.py"
    if not dest.exists():
        shutil.copyfile(source, dest)
        dest.chmod(0o444)
        meta = {"sha256": sha, "original_path": str(source.resolve()), "registered_at": time.time(),
                "environment": environment_lock()}
        (dest_dir / f"{sha}.json").write_text(json.dumps(meta, indent=2))
    return sha


def plugin_path(paths: Paths, sha: str) -> Path:
    path = paths.artifacts / "strategies" / f"{sha}.py"
    if not path.exists():
        raise StrategyError(f"plugin {sha} is not registered")
    if sha256_file(path) != sha:
        raise StrategyError(f"plugin {sha} was modified after registration")
    return path
