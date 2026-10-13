"""Release layout: sibling-container mounts, container network, credentials import, release versions."""

import argparse
import io
import json
import subprocess
from pathlib import Path

import pytest

from quant_research import release, runtime
from quant_research.desk import commands
from quant_research.desk import supervisor as sup
from quant_research.desk.config import DeskConfig, NetworkConfig, effective_network

REPO = Path(__file__).resolve().parents[1]


def test_mount_binds_paths_in_development(monkeypatch, tmp_path):
    monkeypatch.delenv("ZQR_DATA_VOLUME", raising=False)
    assert runtime.mount(tmp_path, "/work") == ["-v", f"{tmp_path.resolve()}:/work"]
    assert runtime.mount(tmp_path, "/etc/x", readonly=True) == ["-v", f"{tmp_path.resolve()}:/etc/x:ro"]


def test_mount_uses_volume_subpaths_in_the_release_container(monkeypatch, tmp_path):
    home = tmp_path / "zqr"
    (home / "sessions" / "s1").mkdir(parents=True)
    monkeypatch.setenv("ZQR_DATA_VOLUME", "zqr-data")
    monkeypatch.setenv("ZQR_HOME", str(home))
    assert runtime.mount(home / "sessions" / "s1", "/workspace", readonly=True) == [
        "--mount", "type=volume,src=zqr-data,dst=/workspace,volume-subpath=sessions/s1,readonly"]
    with pytest.raises(RuntimeError, match="outside ZQR_HOME"):
        runtime.mount(tmp_path / "elsewhere", "/x")
    with runtime.scratch_dir("qr-test-") as d:
        assert d.parent == home / "tmp" and runtime.mount(d, "/w")[1].endswith(f"volume-subpath=tmp/{d.name}")
    assert not d.exists()


def test_container_network_only_with_a_control_network(monkeypatch):
    cfg = DeskConfig(network=NetworkConfig(mode="wsl_eth0", gateway_bind="172.20.0.5", forward_host="172.20.0.5"))
    monkeypatch.delenv("ZQR_CONTROL_NETWORK", raising=False)
    assert effective_network(cfg).mode == "wsl_eth0"
    monkeypatch.setenv("ZQR_CONTROL_NETWORK", "zqr-control")
    monkeypatch.setattr(runtime, "own_address", lambda: "10.9.0.2")
    net = effective_network(cfg)
    assert (net.mode, net.gateway_bind, net.forward_host, net.control_network) == (
        "container", "0.0.0.0", "10.9.0.2", "zqr-control")


def test_secrets_stdin_keeps_only_model_variables(monkeypatch, tmp_path, capsys):
    path = tmp_path / "secrets" / "desk.env"
    monkeypatch.setattr(sup, "CREDENTIAL_FILE", path)
    monkeypatch.setattr("sys.stdin", io.StringIO(
        "export ANTHROPIC_AUTH_TOKEN='sk-test'\nANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic\n"
        "AWS_SECRET_ACCESS_KEY=nope\n# comment\n"))
    commands.cmd_secrets(argparse.Namespace(stdin=True, status=False))
    out = capsys.readouterr().out
    assert "sk-test" not in out and json.loads(out)["names"] == ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert sup.read_env_file(path) == {"ANTHROPIC_AUTH_TOKEN": "sk-test",
                                       "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"}


def test_host_lifecycle_commands_refuse_inside_the_release_container(monkeypatch, tmp_path):
    monkeypatch.setenv("ZQR_DATA_VOLUME", "zqr-data")
    with pytest.raises(SystemExit, match="zqr up"):
        commands.cmd_up(argparse.Namespace(home=str(tmp_path)))
    with pytest.raises(SystemExit, match="development machine"):
        release.release("local")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_release_version_is_commit_based_and_refuses_dirty_image_inputs(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / "notes.md").write_text("draft\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "pyproject.toml", "src")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    monkeypatch.setenv("ZQR_ROOT", str(tmp_path))
    sha = subprocess.run(["git", "rev-parse", "--short=8", "HEAD"], cwd=tmp_path, capture_output=True,
                         text=True).stdout.strip()
    assert release.release_version() == f"1.2.3-{sha}"  # untracked notes outside the image inputs don't count
    (tmp_path / "src" / "a.py").write_text("x = 2\n")
    with pytest.raises(SystemExit, match="uncommitted"):
        release.release_version()
    assert release.release_version(allow_dirty=True).startswith(f"1.2.3-{sha}-dirty")


def test_runtime_image_never_receives_benchmark_answers_or_data():
    rules = [line.strip() for line in (REPO / ".dockerignore").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    assert rules[0] == "*" and "benchmark/evaluator_private" in rules
    assert not any(r.startswith("!var") or r == "!benchmark/evaluator_private" for r in rules)
    assert rules.index("benchmark/evaluator_private") > rules.index("!benchmark")
