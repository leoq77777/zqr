import json
import os
import sys
import time
from datetime import UTC, datetime

from quant_research.desk.config import DeskConfig
from quant_research.desk.supervisor import DeskSupervisor, child_env, read_env_file, state_file
from quant_research.paths import get_paths

SLEEPER = [sys.executable, "-c", "import signal, sys, time\n"
           "signal.signal(signal.SIGTERM, lambda *a: (print('graceful stop', flush=True), sys.exit(0)))\n"
           "print('up', flush=True)\n"
           "while True: time.sleep(0.1)\n"]
CRASHER = [sys.executable, "-c", "print('boom: bad config', flush=True); raise SystemExit(3)"]


def _wait(pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_crashing_child_restarts_with_backoff_and_others_keep_running(tmp_path):
    paths = get_paths(tmp_path / "home")
    sup = DeskSupervisor(paths, DeskConfig(), commands={"fleet": SLEEPER, "research": CRASHER},
                         env_for=lambda n: dict(os.environ), backoff_base=0.2, backoff_max=1.0)
    sup.poll()
    fleet, research = sup.children["fleet"], sup.children["research"]
    assert _wait(lambda: research.proc.poll() is not None)
    sup.poll()
    assert research.restarts == 1 and research.last_exit == 3 and "boom" in research.last_error
    assert research.proc is None and research.next_start > time.time()
    assert _wait(lambda: (sup.poll(), research.restarts >= 3)[1])
    assert research.quick_failures >= 3
    assert fleet.proc.poll() is None and fleet.restarts == 0
    st = sup.write_state()
    assert st["components"]["fleet"]["status"] == "running"
    assert json.loads(state_file(paths).read_text())["components"]["research"]["restarts"] >= 3

    sup.stop()
    assert fleet.last_exit == 0
    assert "graceful stop" in fleet.log_path.read_text()
    assert json.loads(state_file(paths).read_text())["status"] == "stopped"


def test_daily_report_once_per_day_after_the_configured_time(tmp_path):
    paths = get_paths(tmp_path / "home")
    t = {"now": datetime(2026, 10, 12, 0, 1, tzinfo=UTC).timestamp()}
    days = []
    sup = DeskSupervisor(paths, DeskConfig(report_hour_utc=0, report_minute_utc=5), commands={},
                         clock=lambda: t["now"], report=days.append)
    assert sup.maybe_report() is None
    t["now"] += 5 * 60
    assert sup.maybe_report() == "2026-10-11"
    t["now"] += 3600
    assert sup.maybe_report() is None
    t["now"] += 86400
    assert sup.maybe_report() == "2026-10-12"
    assert days == ["2026-10-11", "2026-10-12"]

    def broken(day):
        raise RuntimeError("report bug")

    sup.report = broken
    t["now"] += 86400
    sup.maybe_report()
    assert "report bug" in sup.write_state()["last_report_error"]


def test_only_the_research_child_gets_model_credentials(tmp_path, monkeypatch):
    creds = tmp_path / "desk.env"
    creds.write_text("# model\nexport ANTHROPIC_AUTH_TOKEN='secret-value'\nANTHROPIC_BASE_URL=https://example\n")
    assert read_env_file(creds) == {"ANTHROPIC_AUTH_TOKEN": "secret-value", "ANTHROPIC_BASE_URL": "https://example"}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited")
    research = child_env("research", creds)
    fleet = child_env("fleet", creds)
    assert research["ANTHROPIC_AUTH_TOKEN"] == "secret-value"
    assert not any(k.startswith("ANTHROPIC_") for k in fleet)
