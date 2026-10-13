"""`quant` command line: data -> snapshot -> split -> backtest/validate -> report/replay, agent, paper."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shlex
import shutil
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant_research.backtest.engine import CostConfig, EngineConfig
from quant_research.experiments.runner import ExperimentRequest
from quant_research.experiments.store import ExperimentStore
from quant_research.paths import Paths, get_paths
from quant_research.portfolio.policy import PortfolioPolicy
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.strategy.spec import StrategySpec, load_spec_file
from quant_research.timeutil import parse_granularity, parse_time, to_iso


def _out(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _ctx(args) -> tuple[Paths, ExperimentStore]:
    paths = get_paths(args.home)
    return paths, ExperimentStore(paths.db)


def _client(args):
    from quant_research.data.coinbase import CoinbasePublicClient, RateLimiter

    return CoinbasePublicClient(limiter=RateLimiter(args.rps))


def _engine(args) -> EngineConfig:
    costs = CostConfig(fee_rate=args.fee_rate, slippage_bps=args.slippage_bps,
                       source=args.cost_source or CostConfig().source)
    return EngineConfig(initial_cash=args.initial_cash, costs=costs,
                        base_increment=getattr(args, "base_increment", None))


def _policy(args) -> PortfolioPolicy:
    raw = getattr(args, "policy", None)
    if not raw:
        return PortfolioPolicy()
    text = Path(raw).read_text() if not raw.lstrip().startswith("{") else raw
    return PortfolioPolicy.model_validate_json(text)


def _spec(args, paths: Paths, validate: bool = True) -> StrategySpec:
    from quant_research.strategy.plugin import register_plugin

    if args.spec:
        return load_spec_file(Path(args.spec), register_plugin=lambda p: register_plugin(paths, p))
    params = json.loads(args.params or "{}")
    if args.plugin:
        return StrategySpec(strategy_ref=f"plugin:{register_plugin(paths, Path(args.plugin))}", parameters=params)
    if args.template:
        spec = StrategySpec(strategy_ref=f"template:{args.template}", parameters=params)
        return spec.normalized() if validate else spec
    raise SystemExit("give --spec, --template or --plugin")


def _sandbox(args) -> SandboxConfig:
    return SandboxConfig(isolation=getattr(args, "isolation", "subprocess"))


def _request(args, paths: Paths, segment: str = "dev", validate: bool = True) -> ExperimentRequest:
    spec = _spec(args, paths, validate)
    return ExperimentRequest(strategy_ref=spec.strategy_ref, parameters=spec.parameters, dataset_id=args.dataset,
                             split_id=args.split, segment=segment, engine_config=_engine(args),
                             policy=_policy(args), allow_gaps=getattr(args, "allow_gaps", False))


# -- data ----------------------------------------------------------------------------------------------
def cmd_fetch(args) -> None:
    from quant_research.data.coinbase import fetch_range

    paths = get_paths(args.home)
    end = parse_time(args.end) if args.end else int(time.time())
    s = fetch_range(_client(args), args.product, args.granularity, parse_time(args.start), end, paths.raw)
    _out({k: v for k, v in s.__dict__.items() if k != "files"} | {"start_iso": to_iso(s.start),
                                                                  "end_iso": to_iso(s.end)})


def _validated(args, paths: Paths):
    from quant_research.data.coinbase import VENUE, raw_dir_for
    from quant_research.data.validate import load_raw_candles, validate_bars

    raw_dir = raw_dir_for(paths.raw, args.product, args.granularity)
    end = parse_time(args.end) if args.end else int(time.time())
    raw = load_raw_candles(raw_dir)
    bars, rep = validate_bars(raw, VENUE, args.product, args.granularity, parse_time(args.start), end,
                              fill_policy=args.fill_policy)
    return bars, rep, raw_dir


def cmd_validate(args) -> None:
    paths = get_paths(args.home)
    _, rep, raw_dir = _validated(args, paths)
    (raw_dir / "validation_report.json").write_text(rep.model_dump_json(indent=2))
    out = rep.model_dump()
    out["gaps"] = out["gaps"][:20] + ([f"... {len(rep.gaps) - 20} more"] if len(rep.gaps) > 20 else [])
    out["actual_first_iso"] = to_iso(rep.actual_first) if rep.actual_first else None
    out["actual_last_iso"] = to_iso(rep.actual_last) if rep.actual_last else None
    _out(out)


def cmd_freeze(args) -> None:
    from quant_research.data.snapshot import freeze_dataset

    paths = get_paths(args.home)
    bars, rep, raw_dir = _validated(args, paths)
    m = freeze_dataset(paths, bars, rep, raw_dir)
    _out({k: m[k] for k in ("dataset_id", "data_hash", "rows", "coverage", "internal_gap_bars", "fill_policy",
                            "filled_rows", "has_unfilled_gaps")}
         | {"actual_first": to_iso(m["actual_first_open"]), "actual_last": to_iso(m["actual_last_open"])})


def cmd_make_synthetic(args) -> None:
    from quant_research.data.synthetic import SyntheticSpec, freeze_synthetic

    spec = SyntheticSpec(kind=args.kind, instrument=args.instrument, seed=args.seed, bars=args.bars, vol=args.vol,
                         drift=args.drift, mean_regime_bars=args.regime_bars)
    m = freeze_synthetic(get_paths(args.home), spec)
    _out({"dataset_id": m["dataset_id"], "rows": m["rows"],
          "answer_key": str(get_paths(args.home).root / "controls" / f"{m['dataset_id']}.json")})


def cmd_datasets(args) -> None:
    from quant_research.data.snapshot import list_datasets

    _out([{k: m[k] for k in ("dataset_id", "rows", "coverage", "internal_gap_bars", "fill_policy")}
          | {"first": to_iso(m["actual_first_open"]), "last": to_iso(m["actual_last_open"])}
          for m in list_datasets(get_paths(args.home))])


def cmd_split(args) -> None:
    from quant_research.validation.splits import create_split

    paths = get_paths(args.home)
    s = create_split(paths, args.dataset, parse_time(args.dev_end) if args.dev_end else None,
                     parse_time(args.validation_end) if args.validation_end else None)
    _out({"split_id": s.split_id, "segments": s.describe()})


def cmd_splits(args) -> None:
    from quant_research.validation.splits import list_splits

    _out([{"split_id": s.split_id, "dataset_id": s.dataset_id, "segments": s.describe()}
          for s in list_splits(get_paths(args.home))])


# -- research ------------------------------------------------------------------------------------------
def _summ(res) -> dict[str, Any]:
    d = res.model_dump(exclude={"result_dir"})
    if d.get("metrics"):
        d["metrics"] = {k: d["metrics"][k] for k in ("total_return", "cagr", "sharpe", "max_drawdown", "n_fills",
                                                     "n_round_trips", "exposure", "total_fees")}
    if d.get("statistics"):
        d["statistics"] = {k: d["statistics"][k] for k in ("psr_0", "dsr", "n_trials", "sharpe_ci_05", "sharpe_ci_95")
                           if k in d["statistics"]}
    if d.get("diagnosis"):
        a = d["diagnosis"]["attribution"]
        d["diagnosis"] = {"labels": res.labels,
                          **{k: a[k] for k in ("gross_total_return", "cost_drag", "beta", "alpha_annual")}}
    return d


def cmd_backtest(args) -> None:
    from quant_research.experiments.runner import run_experiment
    from quant_research.report.markdown import write_experiment_report

    paths, store = _ctx(args)
    res = run_experiment(paths, store, _request(args, paths, args.segment), sandbox=_sandbox(args))
    out = _summ(res)
    if args.report and res.has_result:
        out["report"] = str(write_experiment_report(paths, store, res.experiment_id))
    _out(out)


def cmd_grid(args) -> None:
    from quant_research.experiments.runner import run_many
    from quant_research.validation.walkforward import expand_grid

    paths, store = _ctx(args)
    grid = json.loads(args.grid)
    base = _request(args, paths, args.segment, validate=False)
    reqs = [base.model_copy(update={"parameters": p}) for p in expand_grid(grid)]
    store.record_grid(base.strategy_ref, grid, len(reqs))
    results = run_many(paths, store, reqs, workers=args.workers, kind="grid", sandbox=_sandbox(args))
    rows = sorted(({"experiment_id": r.experiment_id, "parameters": q.parameters, "status": r.status,
                    "sharpe": r.metrics["sharpe"] if r.metrics else None,
                    "total_return": r.metrics["total_return"] if r.metrics else None}
                   for r, q in zip(results, reqs, strict=True)), key=lambda x: -(x["sharpe"] or -1e9))
    _out({"candidates": len(reqs), "cumulative_trials_on_split": store.trial_count(split_id=args.split),
          "results": rows})


def cmd_robustness(args) -> None:
    from quant_research.validation.robustness import run_robustness

    paths, store = _ctx(args)
    _out(run_robustness(paths, store, _request(args, paths, args.segment), parent_id=args.parent,
                        subperiods=args.subperiods, sandbox=_sandbox(args)))


def cmd_walkforward(args) -> None:
    from quant_research.validation.walkforward import walk_forward

    paths, store = _ctx(args)
    _out(walk_forward(paths, store, _request(args, paths, validate=False), json.loads(args.grid), args.train_bars,
                      args.test_bars,
                      through=args.through, metric=args.metric, sandbox=_sandbox(args)))


def cmd_freeze_strategy(args) -> None:
    paths, store = _ctx(args)
    spec = _spec(args, paths).normalized()
    store.freeze_candidate(spec.candidate_hash(), spec.strategy_ref, spec.parameters, args.note or "")
    _out({"frozen": spec.candidate_hash(), "strategy_ref": spec.strategy_ref, "parameters": spec.parameters})


def cmd_holdout(args) -> None:
    from quant_research.report.markdown import write_experiment_report
    from quant_research.validation.holdout import run_holdout

    paths, store = _ctx(args)
    res, prior = run_holdout(paths, store, _request(args, paths), sandbox=_sandbox(args))
    out = _summ(res) | {"prior_holdout_runs_on_split": prior}
    if res.has_result:
        out["report"] = str(write_experiment_report(paths, store, res.experiment_id))
    _out(out)


def cmd_experiments(args) -> None:
    _, store = _ctx(args)
    filters = {k: v for k, v in (("split_id", args.split), ("session_id", args.session), ("status", args.status))
               if v}
    rows = store.list_experiments(limit=args.limit, **filters)
    _out([{"id": r["id"], "status": r["status"], "kind": r["kind"], "segment": r["segment"],
           "strategy_ref": r["strategy_ref"][:40], "parameters": r["parameters"],
           "sharpe": (r["metrics"] or {}).get("sharpe"), "total_return": (r["metrics"] or {}).get("total_return"),
           "failure": (r["failure_reason"] or "")[:120]} for r in rows])


def cmd_show(args) -> None:
    _, store = _ctx(args)
    _out(store.get_experiment(args.experiment_id))


def cmd_report(args) -> None:
    from quant_research.report.markdown import write_experiment_report

    paths, store = _ctx(args)
    print(write_experiment_report(paths, store, args.experiment_id))


def cmd_replay(args) -> None:
    from quant_research.experiments.replay import replay_experiment

    paths, store = _ctx(args)
    out = replay_experiment(paths, store, args.experiment_id, sandbox=_sandbox(args))
    _out({k: v for k, v in out.items() if k != "metrics"})
    if not out["reproducible"]:
        sys.exit(1)


# -- agent controller ----------------------------------------------------------------------------------
def _runner(spec: dict[str, Any]):
    from quant_research.controller.agent import CommandRunner, ManualRunner

    if spec["type"] == "command":
        return CommandRunner(spec["command"], timeout_s=spec.get("timeout_s", 3600), pass_env=spec.get("pass_env"))
    return ManualRunner()


def cmd_research_start(args) -> None:
    from quant_research.controller.agent import CLAUDE_PASS_ENV, claude_command
    from quant_research.controller.budget import AcceptanceCriteria, Budget, ModelPricing, pricing_preset
    from quant_research.controller.controller import ResearchController, SessionConfig

    paths, store = _ctx(args)
    runner_spec: dict[str, Any] = {"type": args.runner}
    extra_env = args.pass_env.split(",") if args.pass_env else []
    if args.runner == "command":
        if not args.command:
            raise SystemExit("--runner command needs --command, e.g. --command 'my-agent --prompt-file {task_file}'")
        runner_spec.update(command=shlex.split(args.command), timeout_s=args.agent_timeout, pass_env=extra_env)
    elif args.runner == "claude":
        if shutil.which("claude") is None:
            raise SystemExit("claude CLI not found on PATH")
        runner_spec.update(type="command", preset="claude", command=claude_command(args.agent_model),
                           timeout_s=args.agent_timeout, pass_env=sorted(set(CLAUDE_PASS_ENV + extra_env)))
    if args.pricing:
        pricing = pricing_preset(args.pricing, args.cny_per_usd)
    elif args.model:
        pricing = ModelPricing(model=args.model, input_per_mtok=args.input_price, output_per_mtok=args.output_price,
                               cache_read_per_mtok=args.cache_read_price, source="command line")
    else:
        pricing = None
    cfg = SessionConfig(
        question=args.question, dataset_id=args.dataset, split_id=args.split,
        budget=Budget(max_rounds=args.max_rounds, max_candidates=args.max_candidates,
                      max_experiments=args.max_experiments, max_wall_seconds=args.max_hours * 3600,
                      max_model_cost_usd=args.max_cost, patience_rounds=args.patience,
                      max_trials_per_strategy=args.max_trials_per_strategy or None),
        acceptance=AcceptanceCriteria(min_validation_sharpe=args.min_sharpe,
                                      min_validation_round_trips=args.min_trips,
                                      min_validation_dsr=None if args.min_dsr <= 0 else args.min_dsr),
        engine_config=_engine(args), policy=_policy(args), sandbox=SandboxConfig(isolation=args.isolation),
        allow_weak_isolation=args.allow_weak_isolation, pricing=pricing,
    )
    ctl = ResearchController(paths, store, _runner(runner_spec))
    sid = ctl.create_session(cfg)
    (paths.sessions / sid / "runner.json").write_text(json.dumps(runner_spec))
    summary = ctl.run(sid)
    _out(_session_out(store, sid, summary, paths))


def _session_out(store, sid, summary, paths) -> dict[str, Any]:
    sess = store.get_session(sid)
    return {"session_id": sid, "state": sess["state"], "stop_reason": sess["stop_reason"], "usage": sess["usage"],
            "best": summary.get("best"), "candidates": len(summary.get("candidates", [])),
            "workspace": sess["workspace"], "report": str(paths.reports / sid / "report.md")}


def cmd_research_resume(args) -> None:
    from quant_research.controller.controller import ResearchController

    paths, store = _ctx(args)
    spec = json.loads((paths.sessions / args.session / "runner.json").read_text())
    summary = ResearchController(paths, store, _runner(spec)).run(args.session)
    _out(_session_out(store, args.session, summary, paths))


def cmd_research_stop(args) -> None:
    from quant_research.controller.agent import ManualRunner
    from quant_research.controller.controller import ResearchController

    paths, store = _ctx(args)
    ResearchController(paths, store, ManualRunner()).request_stop(args.session)
    _out(store.get_session(args.session))


def cmd_research_status(args) -> None:
    paths, store = _ctx(args)
    if args.session:
        sess = store.get_session(args.session)
        _out(sess | {"events": store.session_events(args.session)[-15:]})
    else:
        _out([{k: s[k] for k in ("id", "state", "question", "stop_reason")} for s in store.list_sessions()])


def cmd_tool_backtest(args) -> None:
    from quant_research.controller.tools import tool_backtest

    paths, store = _ctx(args)
    _out(tool_backtest(paths, store, args.session, Path(args.spec), args.segment))


def cmd_tool_history(args) -> None:
    from quant_research.controller.tools import tool_history

    _, store = _ctx(args)
    _out(tool_history(store, args.session))


# -- paper ---------------------------------------------------------------------------------------------
def _default_product(product_id: str):
    from quant_research.paper.models import ProductSpec

    base, quote = product_id.split("-")
    return ProductSpec(product_id=product_id, base_currency=base, quote_currency=quote,
                       base_increment=Decimal("0.00000001"), quote_increment=Decimal("0.01"),
                       base_min_size=Decimal("0.00000001"), base_max_size=Decimal("10000"),
                       min_market_funds=Decimal("1"))


def _paper_cfg(args, paths: Paths, instrument: str, granularity: str):
    from quant_research.paper.risk import RiskLimits
    from quant_research.paper.trader import PaperConfig

    spec = _spec(args, paths)
    return PaperConfig(
        name=args.name, instrument=instrument, granularity=granularity, strategy_ref=spec.strategy_ref,
        parameters=spec.parameters, policy=_policy(args), initial_quote=Decimal(str(args.initial_cash)),
        fee_rate=Decimal(str(args.fee_rate)), slippage_bps=Decimal(str(args.slippage_bps)),
        risk=RiskLimits(max_order_notional=args.max_order_notional, max_daily_loss_fraction=args.max_daily_loss,
                        max_orders_per_hour=args.max_orders_per_hour, stale_after_seconds=args.stale_after,
                        kill_switch_file=str(paths.paper / args.name / "KILL")),
        sandbox=_sandbox(args),
    )


def cmd_paper_replay(args) -> None:
    from quant_research.data.snapshot import load_dataset
    from quant_research.paper.runner import run_replay

    paths = get_paths(args.home)
    bars, manifest = load_dataset(paths, args.dataset)
    if args.last_bars:
        bars = bars.tail(args.last_bars + args.warmup_bars).reset_index(drop=True)
    cfg = _paper_cfg(args, paths, manifest["instrument"], manifest["granularity"])
    workdir = paths.paper / args.name
    trader = run_replay(cfg, bars, workdir, _default_product(manifest["instrument"]),
                        warmup_bars=args.warmup_bars, paths=paths,
                        faults=args.faults.split(",") if args.faults else None)
    _out(trader.write_health() | {"orders": len(trader.orders), "workdir": str(workdir)})


def cmd_paper_run(args) -> None:
    from quant_research.paper.models import ProductSpec
    from quant_research.paper.runner import run_live

    paths = get_paths(args.home)
    client = _client(args)
    product = ProductSpec.from_coinbase(client.product(args.product).body)
    cfg = _paper_cfg(args, paths, args.product, parse_granularity(args.granularity))
    trader = run_live(cfg, paths.paper / args.name, client, product, poll_seconds=args.poll_seconds,
                      max_iterations=args.max_iterations, paths=paths)
    _out(trader.write_health())


def cmd_paper_status(args) -> None:
    paths = get_paths(args.home)
    health = paths.paper / args.name / "health.json"
    _out(json.loads(health.read_text()) if health.exists() else {"error": "no health file"})


def cmd_observe_ws(args) -> None:
    from quant_research.paper.ws_observer import observe

    paths = get_paths(args.home)
    out = paths.root / "ws_observations" / time.strftime("%Y%m%dT%H%M%S")
    _out(asyncio.run(observe(args.products.split(","), args.minutes * 60, out)) | {"dir": str(out)})


# -- readiness / benchmark -----------------------------------------------------------------------------
def cmd_readiness(args) -> None:
    from quant_research import readiness

    paths = get_paths(args.home)
    if args.action == "show":
        _out(readiness.load(paths.readiness))
    elif args.action == "probe":
        client = _client(args)
        lat = readiness.probe_rest(client, args.product, args.requests)
        cov = readiness.probe_coverage(client, args.product, args.granularity, parse_time(args.start))
        status = "pass" if lat["errors"] == 0 else "fail"
        readiness.set_check(paths.readiness, "coinbase", "network_measured", status,
                            f"{args.requests} public candle requests from this network", {"latency": lat,
                                                                                          "coverage": cov})
        _out({"latency": lat, "coverage": cov})
    else:
        _out(readiness.set_check(paths.readiness, args.venue, args.check, args.status, args.evidence or ""))


def cmd_benchmark(args) -> None:
    from quant_research.benchmark import run_benchmark

    result = run_benchmark(args.assets, args.years, args.candidates, args.workers)
    paths = get_paths(args.home)
    out = paths.root / "benchmarks" / f"{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    _out(result | {"saved": str(out)})


# -- parser --------------------------------------------------------------------------------------------
def _add_strategy(p) -> None:
    p.add_argument("--spec", help="strategy JSON file")
    p.add_argument("--template")
    p.add_argument("--plugin", help="trusted local plugin file")
    p.add_argument("--params", help="JSON parameters")


def _add_engine(p) -> None:
    p.add_argument("--initial-cash", type=float, default=10_000.0)
    p.add_argument("--fee-rate", type=float, default=0.006, help="taker fee fraction (use your account tier)")
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--cost-source", help="where the cost numbers come from")
    p.add_argument("--base-increment", type=float, help="product quantity step (default: continuous)")
    p.add_argument("--policy", help='portfolio policy JSON or file, e.g. \'{"vol_target": 0.4}\' (default: no vol '
                                    "target, asset cap 1, gross cap 1)")
    p.add_argument("--isolation", choices=["subprocess", "docker"], default="subprocess")


def _add_data_range(p) -> None:
    p.add_argument("--product", required=True, help="e.g. BTC-USD")
    p.add_argument("--granularity", required=True, help="1h, 1d, ONE_HOUR ...")
    p.add_argument("--start", required=True, help="YYYY-MM-DD, ISO time or epoch seconds (UTC)")
    p.add_argument("--end", help="exclusive; default now")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="quant", description=__doc__)
    ap.add_argument("--home", help="runtime directory (default $QUANT_HOME or ./var)")
    ap.add_argument("--rps", type=float, default=5.0, help="shared request rate limit for public APIs")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch-data", help="download Coinbase candles window by window (resumable)")
    _add_data_range(p)
    p.set_defaults(fn=cmd_fetch)
    for name, fn, help_ in (("validate-data", cmd_validate, "quality report for downloaded candles"),
                            ("freeze-dataset", cmd_freeze, "validate and write an immutable snapshot")):
        p = sub.add_parser(name, help=help_)
        _add_data_range(p)
        p.add_argument("--fill-policy", choices=["none", "ffill_flat"], default="none")
        p.set_defaults(fn=fn)
    sub.add_parser("datasets", help="list snapshots").set_defaults(fn=cmd_datasets)
    p = sub.add_parser("make-synthetic", help="control dataset with known ground truth (venue 'lab')")
    p.add_argument("--kind", choices=["random_walk", "regime_trend"], required=True)
    p.add_argument("--instrument", required=True, help="neutral name so agents cannot tell controls apart")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--bars", type=int, default=2110)
    p.add_argument("--vol", type=float, default=0.03)
    p.add_argument("--drift", type=float, default=0.0)
    p.add_argument("--regime-bars", type=float, default=30.0)
    p.set_defaults(fn=cmd_make_synthetic)
    p = sub.add_parser("create-split", help="dev/validation/holdout split (default 60/20/20 by time)")
    p.add_argument("--dataset", required=True)
    p.add_argument("--dev-end")
    p.add_argument("--validation-end")
    p.set_defaults(fn=cmd_split)
    sub.add_parser("splits").set_defaults(fn=cmd_splits)

    def research_cmd(name, fn, help_, segment=True):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--dataset", required=True)
        p.add_argument("--split", required=True)
        _add_strategy(p)
        _add_engine(p)
        p.add_argument("--allow-gaps", action="store_true")
        if segment:
            p.add_argument("--segment", choices=["dev", "validation"], default="dev")
        p.set_defaults(fn=fn)
        return p

    research_cmd("backtest", cmd_backtest, "run one recorded experiment").add_argument("--report", action="store_true")
    p = research_cmd("grid", cmd_grid, "parameter grid on one segment")
    p.add_argument("--grid", required=True, help='e.g. {"fast":[5,10],"slow":[50,100]}')
    p.add_argument("--workers", type=int, default=1)
    p = research_cmd("robustness", cmd_robustness, "cost doubling, neighbour params, sub-periods")
    p.add_argument("--subperiods", type=int, default=4)
    p.add_argument("--parent")
    p = research_cmd("walk-forward", cmd_walkforward, "rolling parameter selection", segment=False)
    p.add_argument("--grid", required=True)
    p.add_argument("--train-bars", type=int, required=True)
    p.add_argument("--test-bars", type=int, required=True)
    p.add_argument("--through", choices=["dev", "validation"], default="dev")
    p.add_argument("--metric", default="sharpe")
    research_cmd("holdout-test", cmd_holdout, "final test on holdout (frozen candidates only)", segment=False)

    p = sub.add_parser("freeze-strategy", help="freeze a candidate before the holdout test")
    _add_strategy(p)
    p.add_argument("--note")
    p.set_defaults(fn=cmd_freeze_strategy)
    p = sub.add_parser("experiments", help="list recorded experiments (all statuses)")
    p.add_argument("--split")
    p.add_argument("--session")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_experiments)
    for name, fn in (("show", cmd_show), ("report", cmd_report), ("replay", cmd_replay)):
        p = sub.add_parser(name)
        p.add_argument("experiment_id")
        if name == "replay":
            p.add_argument("--isolation", choices=["subprocess", "docker"], default="subprocess")
        p.set_defaults(fn=fn)

    research = sub.add_parser("research", help="agent research sessions").add_subparsers(dest="rcmd", required=True)
    p = research.add_parser("start")
    p.add_argument("--dataset", required=True)
    p.add_argument("--split", required=True)
    p.add_argument("--question", required=True)
    p.add_argument("--runner", choices=["manual", "command", "claude"], default="manual",
                   help="claude: Claude Code preset (passes ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN, measures tokens)")
    p.add_argument("--command", help="agent CLI; placeholders {task_file} {workdir} {session}")
    p.add_argument("--agent-model", help="model passed to the claude preset (default: claude's own setting)")
    p.add_argument("--agent-timeout", type=float, default=3600)
    p.add_argument("--pass-env", help="comma-separated env vars the agent needs (e.g. its own API key)")
    p.add_argument("--max-rounds", type=int, default=5)
    p.add_argument("--max-candidates", type=int, default=20)
    p.add_argument("--max-experiments", type=int, default=300)
    p.add_argument("--max-hours", type=float, default=4)
    p.add_argument("--max-cost", type=float, help="USD model budget (needs --pricing or --model with prices)")
    p.add_argument("--pricing", choices=["deepseek-flash", "deepseek-v4-pro"],
                   help="vendor price preset (CNY, cache-hit price, DeepSeek peak/off-peak schedule)")
    p.add_argument("--cny-per-usd", type=float, default=7.1, help="FX for CNY presets")
    p.add_argument("--model", help="model name for custom USD prices")
    p.add_argument("--input-price", type=float, default=0.0, help="USD per 1M uncached input tokens")
    p.add_argument("--cache-read-price", type=float, help="USD per 1M cache-hit input tokens (default: input price)")
    p.add_argument("--output-price", type=float, default=0.0, help="USD per 1M output tokens")
    p.add_argument("--patience", type=int, default=2)
    p.add_argument("--min-sharpe", type=float, default=0.5)
    p.add_argument("--min-trips", type=int, default=4)
    p.add_argument("--min-dsr", type=float, default=0.9,
                   help="minimum Deflated Sharpe on validation given all trials on the split (0 disables)")
    p.add_argument("--max-trials-per-strategy", type=int, default=50,
                   help="per template/plugin trial cap within the session (0 disables)")
    p.add_argument("--allow-weak-isolation", action="store_true",
                   help="allow plugin candidates with subprocess isolation (trusted code only)")
    _add_engine(p)
    p.set_defaults(fn=cmd_research_start)
    p.set_defaults(isolation="docker")
    for name, fn in (("resume", cmd_research_resume), ("stop", cmd_research_stop)):
        p = research.add_parser(name)
        p.add_argument("session")
        p.set_defaults(fn=fn)
    p = research.add_parser("status")
    p.add_argument("session", nargs="?")
    p.set_defaults(fn=cmd_research_status)

    tool = sub.add_parser("tool", help="fixed tools for the coding agent").add_subparsers(dest="tcmd", required=True)
    p = tool.add_parser("backtest")
    p.add_argument("--session", required=True)
    p.add_argument("--spec", required=True)
    p.add_argument("--segment", choices=["dev", "validation"], default="dev")
    p.set_defaults(fn=cmd_tool_backtest)
    p = tool.add_parser("history")
    p.add_argument("--session", required=True)
    p.set_defaults(fn=cmd_tool_history)

    paper = sub.add_parser("paper", help="paper trading (separate process)").add_subparsers(dest="pcmd", required=True)

    def paper_common(p):
        p.add_argument("--name", required=True)
        _add_strategy(p)
        _add_engine(p)
        p.add_argument("--max-order-notional", type=float, default=5000)
        p.add_argument("--max-daily-loss", type=float, default=0.05)
        p.add_argument("--max-orders-per-hour", type=int, default=6)
        p.add_argument("--stale-after", type=float, default=7200)

    p = paper.add_parser("replay", help="replay a snapshot through the full paper stack")
    p.add_argument("--dataset", required=True)
    p.add_argument("--warmup-bars", type=int, default=200)
    p.add_argument("--last-bars", type=int, help="only replay the most recent N bars")
    p.add_argument("--faults", help="comma list: reject,timeout_after_accept,timeout_before_accept,partial,duplicate")
    paper_common(p)
    p.set_defaults(fn=cmd_paper_replay)
    p = paper.add_parser("run", help="live paper loop on Coinbase public data with local simulated fills")
    p.add_argument("--product", required=True)
    p.add_argument("--granularity", required=True)
    p.add_argument("--poll-seconds", type=float, default=30)
    p.add_argument("--max-iterations", type=int)
    paper_common(p)
    p.set_defaults(fn=cmd_paper_run)
    p = paper.add_parser("status")
    p.add_argument("--name", required=True)
    p.set_defaults(fn=cmd_paper_status)

    p = sub.add_parser("observe-ws", help="observe Coinbase WebSocket heartbeats/disconnects")
    p.add_argument("--products", default="BTC-USD,ETH-USD")
    p.add_argument("--minutes", type=float, default=10)
    p.set_defaults(fn=cmd_observe_ws)

    p = sub.add_parser("readiness", help="venue readiness table")
    p.add_argument("action", choices=["show", "probe", "set"])
    p.add_argument("--product", default="BTC-USD")
    p.add_argument("--granularity", default="ONE_HOUR")
    p.add_argument("--start", default="2021-01-01")
    p.add_argument("--requests", type=int, default=20)
    p.add_argument("--venue", default="coinbase")
    p.add_argument("--check")
    p.add_argument("--status", choices=["pending", "pass", "fail", "n/a"])
    p.add_argument("--evidence")
    p.set_defaults(fn=cmd_readiness)

    p = sub.add_parser("benchmark", help="P5 workload: N assets x years hourly x candidates")
    p.add_argument("--assets", type=int, default=2)
    p.add_argument("--years", type=float, default=5)
    p.add_argument("--candidates", type=int, default=100)
    p.add_argument("--workers", type=int, default=1)
    p.set_defaults(fn=cmd_benchmark)
    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args.fn(args)


if __name__ == "__main__":
    main()
