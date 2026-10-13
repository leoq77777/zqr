"""The agent container cannot reach anything except the model host (through the proxy) and the tool gateway.

Parametrized over every gateway network mode available on this machine (Docker Desktop alias, eth0, docker0);
`zqr desk check` runs the same probe and the research loop only starts when one mode passes.
Needs Docker and the agent/runtime images; the allowed-tunnel case also needs internet access.
"""

import json
import shutil
import subprocess

import pytest

from quant_research.bench.isolation import candidate_networks, run_probe
from quant_research.runtime import agent_image, runtime_image
from tests.test_mcp_gateway import _session


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    ok = all(subprocess.run(["docker", "image", "inspect", i], capture_output=True).returncode == 0
             for i in (agent_image(), runtime_image()))
    return ok and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="docker or agent image unavailable")
@pytest.mark.parametrize("network", candidate_networks(), ids=lambda n: n.mode)
def test_agent_network_only_reaches_model_host_and_gateway(paths, store, dataset, tmp_path, network):
    sid = _session(paths, store, dataset)
    out = run_probe(paths, store, sid, network, ("api.deepseek.com", 443), tmp_path / "probe")
    print(json.dumps(out))
    leaks = [p for p in out["problems"] if "reachable" in p or not ("gateway" in p or "model host" in p)]
    if network.mode != "docker_desktop" and not leaks and out.get("probe", {}).get("gateway_token") != 200:
        pytest.skip(f"{network.mode}: gateway not reachable in this Docker setup ({out['problems'][:2]})")
    assert out["ok"], out["problems"]
    assert any("example.com:443" in d for d in out["egress"]["denied"])
