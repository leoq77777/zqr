"""Per-run agent network: an internal Docker network whose only way out is the egress proxy container.

The agent container joins only the internal network (no route to the internet or the host) and gets
`HTTPS_PROXY=http://egress:3128`. The proxy tunnels to the model API host and nothing else, and forwards
`egress:8080` to the tool gateway on the host. External DNS is disabled in the agent container.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROXY_IMAGE = "quant-research-sandbox:latest"
PROXY_ALIAS, PROXY_PORT, GATEWAY_PORT = "egress", 3128, 8080
SCRIPT = Path(__file__).with_name("egress_proxy.py")


class EgressError(RuntimeError):
    pass


def model_endpoint() -> tuple[str, int]:
    """Host and port of the model API from ANTHROPIC_BASE_URL (only the URL is read, never a credential)."""
    parts = urlsplit(os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com")
    return (parts.hostname or "api.anthropic.com").lower(), parts.port or (443 if parts.scheme != "http" else 80)


def _docker(*args: str, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess:
    out = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and out.returncode != 0:
        raise EgressError(f"docker {args[0]} failed: {out.stderr.strip()[:300]}")
    return out


class EgressNetwork:
    def __init__(self, log_dir: Path, allow: list[tuple[str, int]], gateway_port: int | None,
                 image: str = PROXY_IMAGE, label: str | None = None, forward_host: str = "host.docker.internal"):
        """`forward_host` is where the proxy reaches the gateway: Docker Desktop's host alias, or an address the
        gateway binds on (WSL eth0, docker0) when that alias does not lead to it."""
        self.forward_host = forward_host
        self.id = uuid.uuid4().hex[:10]
        self.label = f"quant-research.run={label or self.id}"
        self.network, self.container = f"qr-net-{self.id}", f"qr-egress-{self.id}"
        self.log_dir, self.allow, self.gateway_port, self.image = log_dir, allow, gateway_port, image
        self.log_path = log_dir / "egress.jsonl"

    # what the agent container needs
    def docker_args(self) -> list[str]:
        return ["--network", self.network, "--dns", "127.0.0.1"]

    def env(self) -> dict[str, str]:
        proxy = f"http://{PROXY_ALIAS}:{PROXY_PORT}"
        return {"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy,
                "NO_PROXY": PROXY_ALIAS, "no_proxy": PROXY_ALIAS}

    def gateway_url(self) -> str:
        return f"http://{PROXY_ALIAS}:{GATEWAY_PORT}/mcp"

    def __enter__(self) -> EgressNetwork:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.touch()
        self.log_path.chmod(0o666)
        cmd = ["run", "-d", "--rm", "--name", self.container, "--label", self.label,
               "--add-host", "host.docker.internal:host-gateway",
               "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "256m",
               "--pids-limit", "128", "-v", f"{SCRIPT}:/egress.py:ro", "-v", f"{self.log_dir.resolve()}:/log",
               self.image, "python", "/egress.py", "--listen", str(PROXY_PORT), "--log", "/log/egress.jsonl"]
        for host, port in self.allow:
            cmd += ["--allow", f"{host}:{port}"]
        if self.gateway_port:
            cmd += ["--forward", f"{GATEWAY_PORT}={self.forward_host}:{self.gateway_port}"]
        try:
            _docker("network", "create", "--internal", "--label", self.label, self.network)
            _docker(*cmd)
            _docker("network", "connect", "--alias", PROXY_ALIAS, self.network, self.container)
            deadline = time.time() + 20
            while "egress ready" not in _docker("logs", self.container, check=False).stdout:
                if time.time() > deadline:
                    raise EgressError("egress proxy did not start")
                time.sleep(0.2)
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc) -> None:
        _docker("rm", "-f", self.container, check=False)
        _docker("network", "rm", self.network, check=False)

    def records(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines() if line.strip()]


def cleanup_run(label: str) -> list[str]:
    """Remove containers and networks left behind by a crashed run (matched by the run label)."""
    removed = []
    for kind, ls, rm in (("container", ["ps", "-aq"], ["rm", "-f"]), ("network", ["network", "ls", "-q"],
                                                                      ["network", "rm"])):
        ids = _docker(*ls, "--filter", f"label=quant-research.run={label}", check=False).stdout.split()
        for i in ids:
            _docker(*rm, i, check=False)
            removed.append(f"{kind} {i}")
    return removed


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    denied = [r for r in records if r.get("allowed") is False]
    tunnels = [r for r in records if r.get("kind") == "proxy" and r.get("allowed") and "error" not in r]
    return {"denied": [f"{r.get('method')} {r.get('host')}:{r.get('port')} ({r.get('reason')})" for r in denied],
            "model_tunnels": len(tunnels), "gateway_connections": sum(r.get("kind") == "forward" for r in records),
            "upstream_errors": [f"{r.get('kind')} {r.get('host', r.get('listen'))}: {r['error']}"
                                for r in records if "error" in r],
            "bytes_up": sum(r.get("bytes_up", 0) for r in records),
            "bytes_down": sum(r.get("bytes_down", 0) for r in records)}
