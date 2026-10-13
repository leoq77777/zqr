import shutil
import subprocess

import pytest

from quant_research.runtime import runtime_image
from quant_research.strategy.sandbox import SandboxConfig, SandboxError, run_plugin
from tests.conftest import make_raw


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "image", "inspect", runtime_image()], capture_output=True)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ready(), reason="docker daemon or sandbox image unavailable")


def test_docker_sandbox_runs_plugin_without_network(tmp_path):
    bars = make_raw(50).rename(columns={"start": "ts"})
    ok = tmp_path / "ok.py"
    ok.write_text("def target_positions(bars, params):\n    return [0.5] * len(bars)\n")
    targets, _, _ = run_plugin(ok, bars, {}, SandboxConfig(isolation="docker"))
    assert (targets == 0.5).all()
    net = tmp_path / "net.py"
    net.write_text("import urllib.request\n"
                   "def target_positions(bars, params):\n"
                   "    urllib.request.urlopen('https://example.com', timeout=3)\n"
                   "    return [0.0] * len(bars)\n")
    with pytest.raises(SandboxError):
        run_plugin(net, bars, {}, SandboxConfig(isolation="docker"))
