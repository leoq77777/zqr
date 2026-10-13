"""`quant desk setup` (one-time preparation, idempotent) and `quant desk check` (environment + isolation probe)."""

from __future__ import annotations

import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from quant_research.bench.adapter import AGENT_IMAGE
from quant_research.bench.egress import PROXY_IMAGE, model_endpoint
from quant_research.controller.budget import Budget
from quant_research.desk.config import DeskConfig, check_path, load_config, save_config
from quant_research.desk.presets import preset_roster
from quant_research.desk.promotion import authorization_path, roster_path
from quant_research.desk.supervisor import CREDENTIAL_FILE, read_env_file
from quant_research.experiments.store import ExperimentStore
from quant_research.paper.fleet import save_roster
from quant_research.paths import Paths
from quant_research.research import authorization as auth_mod
from quant_research.research.mandate import Mandate

REPO_ROOT = Path(__file__).resolve().parents[3]
IMAGES = {AGENT_IMAGE: "Dockerfile.agent", PROXY_IMAGE: "Dockerfile.sandbox"}
GRANULARITY = "ONE_HOUR"
DEFAULT_ACCEPTANCE = {"min_primary": 1.0, "min_dsr": 0.9, "min_round_trips": 10, "max_drawdown": -0.25,
                      "require_costs_x2": True, "require_delay": True}
DEFAULT_HOLDOUT = {"max_viewings": 10, "min_primary": 0.5, "min_total_return": 1e-6}
CREDENTIAL_NAMES = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _docker_ok() -> bool:
    return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True,
                                                           timeout=60).returncode == 0


def _image_exists(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


def build_images(rebuild: bool = False) -> dict[str, str]:
    out = {}
    for image, dockerfile in IMAGES.items():
        if _image_exists(image) and not rebuild:
            out[image] = "present"
            continue
        say(f"building {image} from {dockerfile} ...")
        res = subprocess.run(["docker", "build", "-f", dockerfile, "-t", image, "."], cwd=REPO_ROOT)
        out[image] = "built" if res.returncode == 0 else f"build failed ({res.returncode})"
    return out


def prepare_dataset(paths: Paths, product: str, start: int, rps: float = 5.0) -> tuple[dict, Any]:
    from quant_research.data.coinbase import VENUE, CoinbasePublicClient, RateLimiter, fetch_range, raw_dir_for
    from quant_research.data.snapshot import freeze_dataset
    from quant_research.data.validate import load_raw_candles, validate_bars
    from quant_research.validation.splits import create_split

    end = int(time.time()) // 3600 * 3600
    say(f"fetching {product} hourly candles since {time.strftime('%Y-%m-%d', time.gmtime(start))} (resumable) ...")
    fetch_range(CoinbasePublicClient(limiter=RateLimiter(rps)), product, GRANULARITY, start, end, paths.raw)
    raw_dir = raw_dir_for(paths.raw, product, GRANULARITY)
    bars, rep = validate_bars(load_raw_candles(raw_dir), VENUE, product, GRANULARITY, start, end,
                              fill_policy="ffill_flat")
    manifest = freeze_dataset(paths, bars, rep, raw_dir)
    split = create_split(paths, manifest["dataset_id"])
    return manifest, split


def desk_mandate(product: str, dataset_id: str, split_id: str, cfg: DeskConfig) -> Mandate:
    return Mandate.model_validate({
        "question": f"Desk standing question: is there a long-or-cash rule on {product} hourly bars that survives "
                    "costs, a one-bar delay and the multiple-testing penalty of every trial on this split?",
        "dataset_id": dataset_id, "split_id": split_id, "engine_config": {}, "policy": {},
        "evaluation_design": {"kind": "single_validation"}, "primary_metric": "sharpe",
        "acceptance": DEFAULT_ACCEPTANCE,
        "budget": Budget(max_rounds=1, max_candidates=1, max_experiments=12,
                         max_wall_seconds=cfg.session_wall_seconds, max_trials_per_strategy=None),
        "holdout": DEFAULT_HOLDOUT, "agent_segments": ["dev"],
        "notes": "written by `quant desk setup`; thresholds confirmed by the human at setup"})


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise SystemExit(f"{prompt} needs a terminal; re-run interactively or pass --yes")
    return input(f"{prompt} [yes/no]: ").strip().lower() in ("y", "yes")


def enter_credentials(path: Path = CREDENTIAL_FILE) -> str:
    """Hidden input; the value is written to a 600 file and never echoed or logged."""
    if not sys.stdin.isatty():
        return "skipped (no terminal)"
    say("Model credentials for the research child process only (the paper fleet never receives them).")
    base = input(f"ANTHROPIC_BASE_URL [{os.environ.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com')}]: ")
    base = base.strip() or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    token = getpass.getpass("ANTHROPIC_AUTH_TOKEN (hidden): ").strip()
    model = input("ANTHROPIC_MODEL (optional, Enter to skip): ").strip()
    if not token:
        return "skipped (empty token)"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"ANTHROPIC_BASE_URL={base}\nANTHROPIC_AUTH_TOKEN={token}\n")
        if model:
            f.write(f"ANTHROPIC_MODEL={model}\nANTHROPIC_SMALL_FAST_MODEL={model}\n")
    path.chmod(0o600)
    return f"written to {path} (mode 600)"


def run_setup(paths: Paths, *, start: int, skip_images: bool = False, rebuild: bool = False,
              skip_data: bool = False, refresh_data: bool = False, rekey: bool = False, assume_yes: bool = False,
              auth_days: float = 30, auth_max_paper: int = 6) -> dict[str, Any]:
    cfg = load_config(paths)
    report: dict[str, Any] = {}
    if not skip_images:
        if not _docker_ok():
            raise SystemExit("Docker is not available: start Docker Desktop (with WSL integration) and retry")
        report["images"] = build_images(rebuild)

    store = ExperimentStore(paths.db)
    try:
        say("\nDefault desk mandate (one per product, 1h bars, split 60/20/20 by time):\n"
            "  validation: Sharpe >= 1.0, deflated Sharpe >= 0.9 (all trials on the split), >= 10 round trips,\n"
            "              max drawdown not worse than -25%, still profitable with 2x costs and a one-bar delay\n"
            "  holdout:    at most 10 viewings per split; pass = Sharpe >= 0.5 and return > 0\n"
            "  per session: 12 dev experiments, wall clock "
            f"{cfg.session_wall_seconds:.0f}s; daily model cost cap {cfg.daily_cost_cap_usd} USD")
        need = [p for p in cfg.products if refresh_data or p not in cfg.mandates
                or store.get_mandate(cfg.mandates[p]) is None]
        if need and not skip_data:
            if not _confirm("Use these thresholds for the desk mandates?", assume_yes):
                raise SystemExit("setup stopped: edit DEFAULT_ACCEPTANCE/DEFAULT_HOLDOUT or write mandates by hand")
            for product in need:
                manifest, split = prepare_dataset(paths, product, start)
                m = desk_mandate(product, manifest["dataset_id"], split.split_id, cfg)
                store.put_mandate(m.mandate_id(), m.model_dump(mode="json"))
                cfg.mandates[product] = m.mandate_id()
                report.setdefault("data", {})[product] = {"dataset_id": manifest["dataset_id"],
                                                          "rows": manifest["rows"], "split_id": split.split_id,
                                                          "mandate_id": m.mandate_id()}
        report["mandates"] = dict(cfg.mandates)
    finally:
        store.close()

    if not roster_path(paths).exists():
        save_roster(roster_path(paths), preset_roster())
        report["roster"] = "preset roster written"
    else:
        report["roster"] = "kept existing roster"

    if rekey or not CREDENTIAL_FILE.exists():
        report["credentials"] = enter_credentials()
    else:
        report["credentials"] = f"kept {CREDENTIAL_FILE}"

    auth = auth_mod.load(authorization_path(paths))
    valid = False
    if auth is not None:
        try:
            auth.check()
            valid = True
        except auth_mod.AuthorizationError:
            pass
    if not valid:
        from quant_research.desk.commands import interactive_authorize

        report["authorization"] = interactive_authorize(paths, cfg.products, auth_max_paper, auth_days,
                                                        "desk setup", assume_yes)["status"]
    else:
        report["authorization"] = "kept (valid)"
    save_config(paths, cfg)
    report["next"] = ["quant desk check", "quant desk up"]
    return report


# -- check -----------------------------------------------------------------------------------------------
def _coinbase_time_skew() -> float:
    with urllib.request.urlopen("https://api.coinbase.com/api/v3/brokerage/time", timeout=10) as r:
        body = json.loads(r.read())
    return time.time() - float(body["epochSeconds"])


def _credentials_present() -> dict[str, bool]:
    env_file = read_env_file(CREDENTIAL_FILE)
    return {name: bool(os.environ.get(name) or env_file.get(name)) for name in CREDENTIAL_NAMES}


def _model_endpoint() -> tuple[str, int]:
    base = read_env_file(CREDENTIAL_FILE).get("ANTHROPIC_BASE_URL")
    if base and not os.environ.get("ANTHROPIC_BASE_URL"):
        os.environ["ANTHROPIC_BASE_URL"] = base  # URL only; credentials are never loaded here
    return model_endpoint()


def run_check(paths: Paths, *, skip_isolation: bool = False) -> dict[str, Any]:
    from quant_research.bench.isolation import candidate_networks, run_probe
    from quant_research.controller.agent import ManualRunner
    from quant_research.controller.controller import ResearchController, SessionConfig, SessionState
    from quant_research.strategy.sandbox import SandboxConfig

    cfg = load_config(paths)
    checks: dict[str, Any] = {"platform": f"{platform.system()} {platform.release()}"}
    problems: list[str] = []
    docker = _docker_ok()
    checks["docker"] = docker
    if not docker:
        problems.append("docker daemon not reachable")
    else:
        checks["images"] = {img: _image_exists(img) for img in IMAGES}
        problems += [f"image {i} missing (quant desk setup)" for i, ok in checks["images"].items() if not ok]
    try:
        skew = _coinbase_time_skew()
        checks["coinbase"] = "reachable"
        checks["clock_skew_seconds"] = round(skew, 2)
        if abs(skew) > 5:
            problems.append(f"system clock is off by {skew:.1f}s (bars are timestamped by Coinbase)")
    except Exception as exc:  # noqa: BLE001
        checks["coinbase"] = f"unreachable: {type(exc).__name__}"
        problems.append("Coinbase public API unreachable")
    free_gb = shutil.disk_usage(paths.root).free / 1e9
    checks["disk_free_gb"] = round(free_gb, 1)
    if free_gb < 5:
        problems.append(f"only {free_gb:.1f} GB free")
    creds = _credentials_present()
    checks["credentials"] = {k: ("set" if v else "missing") for k, v in creds.items()}
    if not any(creds.values()):
        problems.append(f"no model credential in the environment or {CREDENTIAL_FILE}")
    model = _model_endpoint()
    checks["model_endpoint"] = f"{model[0]}:{model[1]}"
    store = ExperimentStore(paths.db)
    try:
        mandates = {p: bool(store.get_mandate(mid)) for p, mid in cfg.mandates.items()}
        checks["mandates"] = mandates
        if not mandates or not all(mandates.values()):
            problems.append("desk mandates missing (quant desk setup)")
        auth = auth_mod.load(authorization_path(paths))
        try:
            if auth is None:
                raise auth_mod.AuthorizationError("missing")
            auth.check()
            checks["authorization"] = "valid"
        except auth_mod.AuthorizationError as exc:
            checks["authorization"] = f"not valid ({exc}); candidates will wait at validated"
        checks["roster"] = roster_path(paths).exists()

        isolation: list[dict[str, Any]] = []
        chosen = None
        if docker and not skip_isolation and mandates and all(checks.get("images", {}).values()):
            m = Mandate.model_validate(store.get_mandate(next(iter(cfg.mandates.values())))["body"])
            ctl = ResearchController(paths, store, ManualRunner())
            sid = ctl.create_session(SessionConfig(mandate=m, sandbox=SandboxConfig(isolation="docker")))
            store.update_session(sid, state=SessionState.RUNNING)
            for net in candidate_networks():
                say(f"isolation probe: {net.mode} (gateway on {net.gateway_bind}, proxy -> {net.forward_host}) ...")
                res = run_probe(paths, store, sid, net, model, paths.desk / "check" / net.mode)
                isolation.append(res)
                if res["ok"]:
                    chosen = net
                    break
            store.update_session(sid, state=SessionState.COMPLETED, stop_reason="desk check isolation probe")
        checks["isolation"] = isolation
    finally:
        store.close()
    if chosen is not None:
        cfg.network = chosen
        save_config(paths, cfg)
    elif not skip_isolation:
        problems.append("isolation probe did not pass in any network mode; research stays off (paper still runs)")
    result = {"ok": not problems, "isolation_ok": chosen is not None, "network": chosen.mode if chosen else None,
              "problems": problems, "checks": checks, "checked_at": time.time()}
    if not skip_isolation:
        check_path(paths).write_text(json.dumps(result, indent=2, default=str))
    return result
