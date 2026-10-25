"""`zqr desk ...` subcommands."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from quant_research.desk import promotion
from quant_research.paths import get_paths
from quant_research.research import authorization as auth_mod


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise SystemExit("refusing to create a standing authorization without a terminal; pass --yes to confirm")
    return input(prompt).strip().lower() == "authorize"


def authorization_summary(auth: auth_mod.Authorization | None, now: float | None = None) -> dict:
    if auth is None:
        return {"status": "missing"}
    try:
        auth.check(now=now)
        status = "valid"
    except auth_mod.AuthorizationError as exc:
        status = f"invalid: {exc}"
    return {"status": status, **auth.model_dump(),
            "expires_in_days": round((auth.expires_at - (now or time.time())) / 86400, 2)}


def interactive_authorize(paths, products: list[str], max_paper: int, days: float, note: str,
                          assume_yes: bool, auto_paper_admitted: bool = False) -> dict:
    print("Standing authorization for the research desk:\n"
          f"  products:                 {', '.join(products)}\n"
          f"  candidate paper slots:    {max_paper}\n"
          f"  valid for:                {days:g} days\n"
          f"  auto paper admitted:      {auto_paper_admitted} (A13.12 super)\n"
          "  allows:  validated -> frozen, final_evaluated -> paper_approved (paper trading only)\n"
          "  super:   admitted -> paper now (no holdout, no next split, no per-version human)\n"
          "  never:   live trading\n", file=sys.stderr)
    if not _confirm("Type 'authorize' to sign: ", assume_yes):
        raise SystemExit("not authorized")
    auth = auth_mod.create(products, max_paper, days, note=note, auto_paper_admitted=auto_paper_admitted)
    auth_mod.save(promotion.authorization_path(paths), auth)
    return authorization_summary(auth)


def cmd_authorize(args) -> None:
    paths = get_paths(args.home)
    path = promotion.authorization_path(paths)
    if args.revoke:
        path.unlink(missing_ok=True)
        _out({"status": "revoked"})
        return
    if not args.renew:
        _out(authorization_summary(auth_mod.load(path)))
        return
    _out(interactive_authorize(paths, args.products.split(","), args.max_paper, args.days, args.note or "",
                               args.yes, auto_paper_admitted=args.super))


def _store(paths):
    from quant_research.experiments.store import ExperimentStore

    return ExperimentStore(paths.db)


def cmd_research_loop(args) -> None:
    from quant_research.bench.adapter import ClaudeContainerAdapter
    from quant_research.desk import research as rl
    from quant_research.desk.config import load_config
    from quant_research.inbox.draft import PROMPT as INBOX_PROMPT

    paths = get_paths(args.home)
    cfg = load_config(paths)
    adapter = ClaudeContainerAdapter(model=cfg.model, memory=cfg.agent_memory, cpus=cfg.agent_cpus, prompt=rl.PROMPT)
    drafter = ClaudeContainerAdapter(model=cfg.model, memory=cfg.agent_memory, cpus=cfg.agent_cpus,
                                     prompt=INBOX_PROMPT)
    loop = rl.ResearchLoop(paths, _store(paths), cfg, adapter, drafter=drafter)
    signal.signal(signal.SIGTERM, lambda *_: loop.request_stop())
    signal.signal(signal.SIGINT, lambda *_: loop.request_stop())
    loop.run(max_sessions=args.max_sessions)


def _recover(paths, cfg) -> list:
    from quant_research.desk.research import recover_desk_runs

    store = _store(paths)
    try:
        return recover_desk_runs(paths, store, cfg)
    finally:
        store.close()


def _report(paths, day: str) -> Path:
    """The daily report; on the last day of a month also the monthly review (with the soft-stop rule)."""
    from datetime import datetime, timedelta

    from quant_research.desk.portfolio_report import write_monthly_report
    from quant_research.desk.report import write_daily_report

    store = _store(paths)
    try:
        out = write_daily_report(paths, store, day)
        if (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).day == 1:
            write_monthly_report(paths, store, day[:7])
        return out
    finally:
        store.close()


def cmd_supervise(args) -> None:
    from quant_research.desk.config import load_config
    from quant_research.desk.supervisor import DeskSupervisor

    paths = get_paths(args.home)
    cfg = load_config(paths)
    sup = DeskSupervisor(paths, cfg, report=lambda day: _report(paths, day), recover=lambda: _recover(paths, cfg))
    sup.run()


def _supervisor_pid(paths) -> int | None:
    from quant_research.desk.supervisor import pid_alive, state_file

    p = state_file(paths)
    if not p.exists():
        return None
    st = json.loads(p.read_text())
    pid = st.get("supervisor_pid")
    return pid if st.get("status") != "stopped" and pid_alive(pid) else None


def _not_in_release_container(what: str) -> None:
    from quant_research.runtime import in_release_container

    if in_release_container():
        raise SystemExit(f"this container is the supervisor; use `zqr {what}` on the host")


def cmd_up(args) -> None:
    from quant_research.desk.supervisor import logs_dir, quant_argv

    _not_in_release_container("up")
    paths = get_paths(args.home)
    pid = _supervisor_pid(paths)
    if pid:
        _out({"status": "already running", "supervisor_pid": pid})
        return
    logs_dir(paths).mkdir(parents=True, exist_ok=True)
    out = open(logs_dir(paths) / "supervisor.out", "a")  # noqa: SIM115 - handed to the child
    proc = subprocess.Popen(quant_argv(paths, "desk", "supervise"), stdout=out, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(3)
    _out({"status": "started" if proc.poll() is None else f"exited {proc.returncode}", "supervisor_pid": proc.pid,
          "logs": str(logs_dir(paths)), "next": "zqr desk status"})


def cmd_down(args) -> None:
    from quant_research.desk.supervisor import pid_alive

    _not_in_release_container("down")
    paths = get_paths(args.home)
    pid = _supervisor_pid(paths)
    if not pid:
        _out({"status": "not running"})
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + args.timeout
    while pid_alive(pid) and time.time() < deadline:
        time.sleep(1)
    _out({"status": "stopped" if not pid_alive(pid) else f"still running after {args.timeout}s", "pid": pid})


def _age(ts: float | None) -> str:
    if not ts:
        return "-"
    s = time.time() - ts
    return f"{s:.0f}s ago" if s < 120 else f"{s / 60:.0f}m ago" if s < 7200 else f"{s / 3600:.1f}h ago"


def desk_status(paths) -> dict:
    from quant_research.campaign import budget
    from quant_research.desk import research as rl
    from quant_research.desk.config import load_check, load_config
    from quant_research.desk.supervisor import state_file
    from quant_research.runtime import version

    cfg = load_config(paths)
    sup = json.loads(state_file(paths).read_text()) if state_file(paths).exists() else {}
    fleet_health = paths.paper / "fleet_health.json"
    fleet = json.loads(fleet_health.read_text()) if fleet_health.exists() else {}
    today = rl.utc_day(time.time())
    ledger = [r for r in rl.read_ledger(paths) if r.get("day") == today]
    sessions = [r for r in ledger if r.get("topic") != rl.INBOX_TOPIC]
    check = load_check(paths)
    return {
        "version": version(),
        "supervisor": {"running": bool(_supervisor_pid(paths)), "pid": sup.get("supervisor_pid"),
                       "status": sup.get("status"), "updated": _age(sup.get("updated_at")),
                       "components": sup.get("components", {}), "last_report_day": sup.get("last_report_day"),
                       "last_report_error": sup.get("last_report_error")},
        "research": {**{k: v for k, v in rl.load_state(paths).items() if k != "updated_at"},
                     "today_sessions": len(sessions), "today_inbox_drafts": len(ledger) - len(sessions),
                     "today_cost_usd": round(sum(r.get("cost_usd") or 0
                                                                               for r in ledger), 4),
                     "daily_cap_usd": budget.limits(cfg)["daily_usd"],
                     "month_cost_usd": round(rl.spent_in_month(paths, budget.month_of(today)), 4),
                     "monthly_cap_usd": budget.limits(cfg)["monthly_usd"],
                     "budget_stop": (budget.active_stop(paths) or {}).get("reason")},
        "fleet": {"updated": _age(fleet.get("ts")), "strategies": len(fleet.get("strategies", {})),
                  "stale": sorted(n for n, h in fleet.get("strategies", {}).items() if h.get("stale")),
                  "halted": sorted(n for n, h in fleet.get("strategies", {}).items() if h.get("halted_reason")),
                  "errors": fleet.get("errors", {})},
        "authorization": authorization_summary(auth_mod.load(promotion.authorization_path(paths)))["status"],
        "check": {"isolation_ok": check.get("isolation_ok"), "checked": _age(check.get("checked_at")),
                  "network": check.get("network")},
    }


def cmd_status(args) -> None:
    paths = get_paths(args.home)
    st = desk_status(paths)
    if args.json:
        _out(st)
        return
    s, r, f = st["supervisor"], st["research"], st["fleet"]
    print(f"version:    {st['version']}")
    print(f"supervisor: {'RUNNING' if s['running'] else 'STOPPED'} (pid {s['pid']}, updated {s['updated']})")
    for name, c in s["components"].items():
        line = f"  {name:9s} {c['status']:8s} pid {c['pid']} restarts {c['restarts']}"
        if c.get("last_error") and c["restarts"]:
            line += f"  last error: {c['last_error'][-160:]}"
        print(line)
    wait = (r.get("resume_at") or 0) - time.time()
    resume = f", resumes in {wait / 60:.0f}m" if wait > 0 else ""
    print(f"research:   {r.get('status', '-')}{resume}  {r.get('reason') or ''}")
    print(f"            today {r['today_sessions']} sessions, {r['today_inbox_drafts']} inbox drafts, "
          f"{r['today_cost_usd']} / {r['daily_cap_usd']} USD; "
          f"started total {r.get('sessions_started', 0)}")
    print(f"fleet:      {f['strategies']} strategies, updated {f['updated']}; stale {f['stale'] or 'none'}; "
          f"halted {f['halted'] or 'none'}; errors {list(f['errors']) or 'none'}")
    print(f"authorization: {st['authorization']}")
    c = st["check"]
    print(f"check:      isolation {'OK' if c['isolation_ok'] else 'NOT PASSED'} ({c['checked']}); "
          f"network {c['network']}")
    if s["last_report_error"]:
        print(f"report:     last error {s['last_report_error']}")


def cmd_panel(args) -> None:
    from quant_research.desk.panel import serve, snapshot

    paths = get_paths(args.home)
    if args.json:
        _out(snapshot(paths) if not args.snapshot else json.loads(Path(args.snapshot).read_text()))
        return
    serve(paths, args.host, args.port, snapshot_path=args.snapshot)


def cmd_logs(args) -> None:
    from quant_research.desk.supervisor import logs_dir

    paths = get_paths(args.home)
    path = logs_dir(paths) / f"{args.component}.log"
    if args.component == "events":
        path = paths.paper / "fleet.jsonl"
    if not path.exists():
        raise SystemExit(f"no log at {path}")
    lines = path.read_text(errors="replace").splitlines()
    print("\n".join(lines[-args.lines:]))
    if args.follow:
        with open(path, errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            while True:
                line = fh.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(1)


def cmd_report(args) -> None:
    from quant_research.desk.research import utc_day

    paths = get_paths(args.home)
    day = args.day or utc_day(time.time())
    path = _report(paths, day)
    if args.print:
        print(path.read_text())
    else:
        _out({"report": str(path)})


def cmd_monthly(args) -> None:
    from quant_research.desk.portfolio_report import write_monthly_report

    paths = get_paths(args.home)
    store = _store(paths)
    try:
        _out({"report": str(write_monthly_report(paths, store, args.month))})
    finally:
        store.close()


def cmd_soft_stop(args) -> None:
    from quant_research.desk import softstop

    paths = get_paths(args.home)
    if args.action == "status":
        _out(softstop.active(paths) or {"status": "none"})
    elif args.action == "evaluate":
        _out(softstop.evaluate(paths, args.day or time.strftime("%Y-%m-%d", time.gmtime())))
    else:
        try:
            _out(softstop.clear(paths, args.note or ""))
        except ValueError as exc:
            raise SystemExit(str(exc)) from None


def cmd_use_mandate(args) -> None:
    from quant_research.desk.config import use_mandate

    paths = get_paths(args.home)
    store = _store(paths)
    try:
        _out(use_mandate(paths, store, args.product, args.mandate, args.note))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        store.close()


def cmd_recover(args) -> None:
    from quant_research.desk.config import load_config

    paths = get_paths(args.home)
    _out(_recover(paths, load_config(paths)))


def cmd_promote(args) -> None:
    from quant_research.strategy.sandbox import SandboxConfig

    paths = get_paths(args.home)
    store = _store(paths)
    try:
        _out(promotion.promote(paths, store, args.candidate_id, SandboxConfig(isolation=args.isolation)))
    finally:
        store.close()


def cmd_setup(args) -> None:
    from quant_research.desk.setup import run_setup
    from quant_research.timeutil import parse_time

    paths = get_paths(args.home)
    _out(run_setup(paths, start=parse_time(args.start), skip_images=args.skip_images, rebuild=args.rebuild,
                   skip_data=args.skip_data, refresh_data=args.refresh_data, rekey=args.rekey, assume_yes=args.yes,
                   auth_days=args.auth_days, auth_max_paper=args.auth_max_paper))


def cmd_secrets(args) -> None:
    """Model credentials for the research child: `--stdin` takes KEY=VALUE lines (only model variables are kept),
    otherwise hidden interactive input. Values are never printed."""
    from quant_research.bench.adapter import MODEL_ENV
    from quant_research.desk.setup import enter_credentials
    from quant_research.desk.supervisor import CREDENTIAL_FILE, read_env_file

    path = CREDENTIAL_FILE
    if args.status:
        _out({"file": str(path), "names": sorted(read_env_file(path))})
        return
    if not args.stdin:
        _out({"credentials": enter_credentials(path), "file": str(path)})
        return
    pairs = {}
    for line in sys.stdin.read().splitlines():
        k, sep, v = line.strip().removeprefix("export ").partition("=")
        if sep and k.strip() in MODEL_ENV and v.strip():
            pairs[k.strip()] = v.strip().strip("'\"")
    if not pairs:
        raise SystemExit("no model variables on stdin")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in pairs.items()))
    _out({"file": str(path), "names": sorted(pairs)})


def cmd_check(args) -> None:
    from quant_research.desk.setup import run_check

    paths = get_paths(args.home)
    res = run_check(paths, skip_isolation=args.skip_isolation)
    if args.json:
        _out(res)
    else:
        for k, v in res["checks"].items():
            if k == "isolation":
                for r in v:
                    print(f"  isolation[{r['network']['mode']}]: {'PASS' if r['ok'] else 'FAIL'} "
                          f"{'; '.join(r['problems'])[:300]}")
            else:
                print(f"  {k}: {v}")
        print(("OK" if res["ok"] else "PROBLEMS:\n  - " + "\n  - ".join(res["problems"]))
              + f"\nresearch loop: {'enabled' if res['isolation_ok'] else 'blocked until isolation passes'}"
              + (f" (network mode {res['network']})" if res["network"] else ""))
    if not res["ok"]:
        raise SystemExit(1)


def register(sub) -> None:
    desk = sub.add_parser("desk", help="research desk: paper fleet + research loop + daily report").add_subparsers(
        dest="dcmd", required=True)
    p = desk.add_parser("setup", help="one-time setup: images, data, mandates, roster, credentials, authorization")
    p.add_argument("--start", default="2021-01-01", help="first day of hourly history")
    p.add_argument("--skip-images", action="store_true")
    p.add_argument("--rebuild", action="store_true", help="rebuild Docker images even if present")
    p.add_argument("--skip-data", action="store_true")
    p.add_argument("--refresh-data", action="store_true",
                   help="new snapshot, split and mandate (resets trial counts and holdout viewings)")
    p.add_argument("--rekey", action="store_true", help="enter model credentials again")
    p.add_argument("--auth-days", type=float, default=30)
    p.add_argument("--auth-max-paper", type=int, default=6)
    p.add_argument("--yes", action="store_true", help="accept the default mandate and sign the authorization")
    p.set_defaults(fn=cmd_setup)
    p = desk.add_parser("check", help="environment checks and the agent-network isolation probe")
    p.add_argument("--skip-isolation", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_check)
    p = desk.add_parser("secrets", help="write the model credentials file (hidden input, or --stdin)")
    p.add_argument("--stdin", action="store_true", help="read KEY=VALUE lines; only model variables are kept")
    p.add_argument("--status", action="store_true", help="show the variable names present")
    p.set_defaults(fn=cmd_secrets)
    p = desk.add_parser("up", help="development: start the supervisor in the background")
    p.set_defaults(fn=cmd_up)
    p = desk.add_parser("down", help="stop the supervisor gracefully")
    p.add_argument("--timeout", type=float, default=300)
    p.set_defaults(fn=cmd_down)
    p = desk.add_parser("supervise", help="run the supervisor in the foreground")
    p.set_defaults(fn=cmd_supervise)
    p = desk.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = desk.add_parser("logs")
    p.add_argument("component", nargs="?", default="supervisor", choices=["supervisor", "fleet", "research", "events"])
    p.add_argument("-n", "--lines", type=int, default=50)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(fn=cmd_logs)
    p = desk.add_parser("report", help="write the daily report (default: today so far, UTC)")
    p.add_argument("--day", help="YYYY-MM-DD (UTC)")
    p.add_argument("--print", action="store_true")
    p.set_defaults(fn=cmd_report)
    p = desk.add_parser("monthly", help="write the monthly review of the paper portfolios")
    p.add_argument("--month", required=True, help="YYYY-MM (UTC)")
    p.set_defaults(fn=cmd_monthly)
    p = desk.add_parser("soft-stop", help="investment policy 2: status, evaluate on a review day, clear (human)")
    p.add_argument("action", choices=("status", "evaluate", "clear"))
    p.add_argument("--day", help="review day YYYY-MM-DD (UTC) for evaluate; default today")
    p.add_argument("--note", help="required for clear")
    p.set_defaults(fn=cmd_soft_stop)
    p = desk.add_parser("use-mandate", help="point a desk product at another stored mandate (the human's decision; "
                                            "recorded in desk/mandate_history.jsonl)")
    p.add_argument("--product", required=True)
    p.add_argument("--mandate", required=True)
    p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_use_mandate)
    p = desk.add_parser("research-loop", help="run the research loop in the foreground (the supervisor's child)")
    p.add_argument("--max-sessions", type=int)
    p.set_defaults(fn=cmd_research_loop)
    p = desk.add_parser("recover", help="close out research sessions of a dead desk process")
    p.set_defaults(fn=cmd_recover)
    p = desk.add_parser("promote", help="push a candidate as far as the authorization allows (resumes from its state)")
    p.add_argument("candidate_id")
    p.add_argument("--isolation", choices=["subprocess", "docker"], default="docker",
                   help="sandbox for the holdout run (the research loop uses docker)")
    p.set_defaults(fn=cmd_promote)
    p = desk.add_parser("authorize", help="show, renew or revoke the standing authorization")
    p.add_argument("--renew", action="store_true")
    p.add_argument("--revoke", action="store_true")
    p.add_argument("--products", default="BTC-USD,ETH-USD")
    p.add_argument("--max-paper", type=int, default=6, help="max candidate strategies on paper at once")
    p.add_argument("--days", type=float, default=30)
    p.add_argument("--note")
    p.add_argument("--super", action="store_true",
                   help="A13.12: admitted versions go to paper immediately")
    p.add_argument("--yes", action="store_true", help="confirm without the interactive prompt")
    p.set_defaults(fn=cmd_authorize)
    p = desk.add_parser("panel", help="paper blotter: equity, fills and research spend in a local page")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--json", action="store_true", help="print the snapshot and exit")
    p.add_argument("--snapshot", type=Path, help="serve a saved snapshot instead of live ledgers")
    p.set_defaults(fn=cmd_panel)
