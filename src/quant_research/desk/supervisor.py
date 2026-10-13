"""Desk supervisor: keeps the paper fleet and the research loop running as child processes.

- Children restart with exponential backoff (reset after a long healthy run).
- Only the research child gets model credentials (the credentials file, `ZQR_CREDENTIALS`, plus the supervisor's own
  environment); the fleet child's environment has every model variable removed.
- The daily report is written once per UTC day after the configured time.
- SIGTERM/SIGINT: stop research first (its running session is ended and recorded), then the fleet.
- `desk/state.json` holds pids, restarts and last errors for `zqr desk status`.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from quant_research.bench.adapter import MODEL_ENV
from quant_research.desk.config import DeskConfig
from quant_research.paths import Paths

log = logging.getLogger(__name__)
HEALTHY_AFTER_S = 600
STOP_ORDER = ("research", "fleet")
STOP_GRACE = {"research": 180.0, "fleet": 30.0}
CREDENTIAL_FILE = Path(os.environ.get("ZQR_CREDENTIALS") or Path.home() / ".config" / "zqr" / "desk.env")


def state_file(paths: Paths) -> Path:
    return paths.desk / "state.json"


def logs_dir(paths: Paths) -> Path:
    return paths.desk / "logs"


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return out


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def quant_argv(paths: Paths, *args: str) -> list[str]:
    return [sys.executable, "-m", "quant_research.cli", "--home", str(paths.root), *args]


def default_commands(paths: Paths) -> dict[str, list[str]]:
    return {"fleet": quant_argv(paths, "paper", "fleet"), "research": quant_argv(paths, "desk", "research-loop")}


def child_env(name: str, credentials: Path = CREDENTIAL_FILE) -> dict[str, str]:
    env = dict(os.environ)
    if name == "research":
        env.update(read_env_file(credentials))
    else:
        for var in MODEL_ENV:
            env.pop(var, None)
    env.setdefault("MPLCONFIGDIR", str(Path("/tmp") / f"mpl-{os.getuid()}"))
    env["PYTHONUNBUFFERED"] = "1"
    return env


@dataclass
class Child:
    name: str
    argv: list[str]
    proc: subprocess.Popen | None = None
    started_at: float | None = None
    restarts: int = 0
    quick_failures: int = 0
    next_start: float = 0.0
    last_exit: int | None = None
    last_error: str | None = None
    log_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        running = self.proc is not None and self.proc.poll() is None
        return {"pid": self.proc.pid if running else None, "status": "running" if running else "waiting",
                "started_at": self.started_at, "restarts": self.restarts, "last_exit": self.last_exit,
                "last_error": self.last_error, "next_start": None if running else self.next_start,
                "log": str(self.log_path)}


class DeskSupervisor:
    def __init__(self, paths: Paths, cfg: DeskConfig, *, commands: dict[str, list[str]] | None = None,
                 env_for: Callable[[str], dict[str, str]] = child_env, clock: Callable[[], float] = time.time,
                 report: Callable[[str], Any] | None = None, recover: Callable[[], Any] | None = None,
                 backoff_base: float = 5.0, backoff_max: float = 300.0):
        self.paths, self.cfg, self.clock, self.env_for = paths, cfg, clock, env_for
        self.report, self.recover = report, recover
        self.backoff_base, self.backoff_max = backoff_base, backoff_max
        logs_dir(paths).mkdir(parents=True, exist_ok=True)
        self.children = {n: Child(n, argv, log_path=logs_dir(paths) / f"{n}.log")
                         for n, argv in (commands or default_commands(paths)).items()}
        self.stopping = False
        self.started_at = clock()
        self.last_report_day = self._load().get("last_report_day")
        self.last_report_error: str | None = None

    # -- state -----------------------------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        p = state_file(self.paths)
        try:
            return json.loads(p.read_text()) if p.exists() else {}
        except ValueError:
            return {}

    def write_state(self, status: str = "running") -> dict[str, Any]:
        state = {"supervisor_pid": os.getpid(), "status": status, "started_at": self.started_at,
                 "updated_at": self.clock(), "components": {n: c.view() for n, c in self.children.items()},
                 "last_report_day": self.last_report_day, "last_report_error": self.last_report_error}
        tmp = state_file(self.paths).with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str))
        tmp.replace(state_file(self.paths))
        return state

    def _log(self, msg: str) -> None:
        with open(logs_dir(self.paths) / "supervisor.log", "a") as f:
            f.write(f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {msg}\n")

    # -- children --------------------------------------------------------------------------------
    def start(self, child: Child) -> None:
        log_f = open(child.log_path, "a")  # noqa: SIM115 - handed to the child
        log_f.write(f"\n=== {datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} starting {child.name} ===\n")
        log_f.flush()
        child.proc = subprocess.Popen(child.argv, stdout=log_f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      env=self.env_for(child.name), start_new_session=True)
        log_f.close()
        child.started_at = self.clock()
        self._log(f"started {child.name} pid {child.proc.pid}")

    def _tail(self, path: Path, n: int = 5) -> str:
        try:
            lines = [x for x in path.read_text(errors="replace").splitlines() if x.strip()]
        except FileNotFoundError:
            return ""
        return " | ".join(lines[-n:])[-600:]

    def poll(self) -> None:
        now = self.clock()
        for child in self.children.values():
            if child.proc is not None:
                code = child.proc.poll()
                if code is None:
                    continue
                ran = now - (child.started_at or now)
                child.last_exit, child.proc = code, None
                child.last_error = self._tail(child.log_path)
                child.quick_failures = 0 if ran >= HEALTHY_AFTER_S else child.quick_failures + 1
                child.restarts += 1
                delay = min(self.backoff_base * 2 ** max(child.quick_failures - 1, 0), self.backoff_max)
                child.next_start = now + delay
                self._log(f"{child.name} exited {code} after {ran:.0f}s; restart in {delay:.0f}s")
            if not self.stopping and child.proc is None and now >= child.next_start:
                self.start(child)

    def stop(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self.stopping = True
        self.write_state("stopping")
        for name in STOP_ORDER:
            child = self.children.get(name)
            if child is None or child.proc is None or child.proc.poll() is not None:
                continue
            self._log(f"stopping {name}")
            child.proc.send_signal(signal.SIGTERM)
            deadline = self.clock() + STOP_GRACE.get(name, 30.0)
            while child.proc.poll() is None and self.clock() < deadline:
                sleep(0.2)
            if child.proc.poll() is None:
                self._log(f"{name} did not stop in time; killing")
                os.killpg(child.proc.pid, signal.SIGKILL)
                child.proc.wait(10)
            child.last_exit = child.proc.returncode
        self.write_state("stopped")
        self._log("supervisor stopped")

    # -- daily report ----------------------------------------------------------------------------
    def maybe_report(self) -> str | None:
        if self.report is None:
            return None
        now = datetime.fromtimestamp(self.clock(), tz=UTC)
        due = now.replace(hour=self.cfg.report_hour_utc, minute=self.cfg.report_minute_utc, second=0, microsecond=0)
        today = now.strftime("%Y-%m-%d")
        if now < due or self.last_report_day == today:
            return None
        day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        self.last_report_day = today
        try:
            self.report(day)
            self.last_report_error = None
            self._log(f"daily report for {day} written")
        except Exception as exc:  # noqa: BLE001 - a report bug must not stop trading or research
            self.last_report_error = f"{type(exc).__name__}: {exc}"
            self._log(f"daily report failed: {self.last_report_error}")
        return day

    # -- main loop -------------------------------------------------------------------------------
    def run(self, max_ticks: int | None = None, tick: float = 2.0, sleep: Callable[[float], None] = time.sleep,
            install_signals: bool = True) -> None:
        if install_signals:
            def _stop(*_):
                self.stopping = True

            signal.signal(signal.SIGTERM, _stop)
            signal.signal(signal.SIGINT, _stop)
        self._log(f"supervisor starting pid {os.getpid()}")
        if self.recover is not None:
            try:
                self._log(f"recovered: {self.recover()}")
            except Exception as exc:  # noqa: BLE001
                self._log(f"recovery failed: {type(exc).__name__}: {exc}")
        ticks = 0
        while not self.stopping and (max_ticks is None or ticks < max_ticks):
            ticks += 1
            self.poll()
            self.maybe_report()
            self.write_state()
            sleep(tick)
        self.stop(sleep)
