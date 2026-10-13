"""Where the software runs: resources, version, images, and how sibling containers see data.

Two layouts share this code:
- development: a source checkout; resources live in the repository, data under `ZQR_HOME` (default `./var`) is
  bind-mounted into sibling containers by path.
- release image: resources under `ZQR_ROOT` (/opt/zqr); the desk runs in a container with the Docker socket, data
  lives in the named volume `ZQR_DATA_VOLUME` mounted at `ZQR_HOME`, and sibling containers (agent, egress proxy,
  strategy sandbox) receive only volume sub-paths. The Docker daemon cannot see paths inside the desk container,
  so every mounted path must live under `ZQR_HOME`.
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path

AGENT_UID = 1000  # `node` in the agent image


def root() -> Path:
    env = os.environ.get("ZQR_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[2]


def benchmark_dir() -> Path:
    env = os.environ.get("ZQR_BENCHMARK_DIR")
    return Path(env) if env else root() / "benchmark"


def lockfile() -> Path:
    return root() / "uv.lock"


def version() -> str:
    return os.environ.get("ZQR_VERSION") or f"{metadata.version('quant-research')}-dev"


def agent_image() -> str:
    return os.environ.get("ZQR_AGENT_IMAGE", "zqr-agent:dev")


def runtime_image() -> str:
    """The release image itself; also runs the egress proxy and the strategy sandbox, so their code always matches
    the desk's version."""
    return os.environ.get("ZQR_IMAGE", "zqr:dev")


def in_release_container() -> bool:
    return bool(os.environ.get("ZQR_DATA_VOLUME"))


def mount(path: Path, dst: str, readonly: bool = False) -> list[str]:
    """`docker run` arguments that make `path` (a directory) appear at `dst` in a sibling container."""
    path = Path(path).resolve()
    volume = os.environ.get("ZQR_DATA_VOLUME")
    if not volume:
        return ["-v", f"{path}:{dst}{':ro' if readonly else ''}"]
    home = Path(os.environ.get("ZQR_HOME", "/zqr")).resolve()
    try:
        sub = path.relative_to(home)
    except ValueError:
        raise RuntimeError(f"{path} is outside ZQR_HOME ({home}); sibling containers only see the data volume") \
            from None
    spec = f"type=volume,src={volume},dst={dst}"
    if sub.parts:
        spec += f",volume-subpath={sub.as_posix()}"
    return ["--mount", spec + (",readonly" if readonly else "")]


@contextmanager
def scratch_dir(prefix: str) -> Iterator[Path]:
    """A temporary directory a sibling container can mount and write (its user is not ours)."""
    if in_release_container():
        base = Path(os.environ.get("ZQR_HOME", "/zqr")) / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{prefix}{uuid.uuid4().hex[:12]}"
        path.mkdir()
        try:
            path.chmod(0o777)
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)
    else:
        with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
            Path(tmp).chmod(0o777)
            yield Path(tmp)


def hand_over(path: Path, uid: int) -> None:
    """Running as root (the release container), give a tree to a container's non-root user."""
    if os.geteuid() != 0:
        return
    for p in [path, *path.rglob("*")]:
        os.chown(p, uid, uid, follow_symlinks=False)


def control_network() -> str | None:
    """User-defined network shared by the desk container and each run's egress proxy (release layout only)."""
    return os.environ.get("ZQR_CONTROL_NETWORK") or None


def own_address() -> str:
    return socket.gethostbyname(socket.gethostname())
