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
                              fill_policy=args.fill_policy, drop_isolated_prints=True)
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


def cmd_extend_dataset(args) -> None:
    from quant_research.data.coinbase import VENUE, raw_dir_for
    from quant_research.data.snapshot import SnapshotError, extend_dataset, load_manifest
    from quant_research.data.validate import load_raw_candles, validate_bars

    paths = get_paths(args.home)
    try:
        base = load_manifest(paths, args.dataset)
        raw_dir = raw_dir_for(paths.raw, base["instrument"], base["granularity"])
        bars, rep = validate_bars(load_raw_candles(raw_dir), VENUE, base["instrument"], base["granularity"],
                                  parse_time(args.start), base["actual_first_open"], fill_policy=args.fill_policy,
                                  drop_isolated_prints=True)
        m = extend_dataset(paths, args.dataset, bars, rep, raw_dir)
    except SnapshotError as exc:
        raise SystemExit(str(exc)) from None
    _out({k: m[k] for k in ("dataset_id", "extends", "rows", "coverage", "internal_gap_bars", "filled_rows",
                            "fill_policy", "junction")}
         | {"actual_first": to_iso(m["actual_first_open"]), "actual_last": to_iso(m["actual_last_open"]),
            "prefix_rows": rep.rows, "prefix_coverage": rep.coverage,
            "prefix_gaps": [{"start": to_iso(g.start), "bars": g.bars} for g in rep.gaps],
            "isolated_prints": [p | {"start": to_iso(p["start"])} for p in rep.isolated_prints]})


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
          | ({"extends": m["extends"]} if m.get("extends") else {})
          for m in list_datasets(get_paths(args.home))])


def cmd_split(args) -> None:
    from quant_research.validation.splits import SplitError, create_split, create_split_like

    paths = get_paths(args.home)
    if args.like:
        if args.dev_end or args.validation_end:
            raise SystemExit("--like takes the other split's boundaries; drop --dev-end/--validation-end")
        try:
            s = create_split_like(paths, args.dataset, args.like)
        except SplitError as exc:
            raise SystemExit(str(exc)) from None
    else:
        s = create_split(paths, args.dataset, parse_time(args.dev_end) if args.dev_end else None,
                         parse_time(args.validation_end) if args.validation_end else None)
    _out({"split_id": s.split_id, "segments": s.describe()})


def cmd_split_lineage(args) -> None:
    from quant_research.validation.splits import SplitError, link_split, load_split

    paths, store = _ctx(args)
    try:
        out = link_split(paths, store, args.split, args.parent, args.note)
    except SplitError as exc:
        raise SystemExit(str(exc)) from None
    _out(out | {s: load_split(paths, sid).describe() for s, sid in (("split", args.split),
                                                                    ("parent", out["lineage"]["parent_id"]))})


def cmd_splits(args) -> None:
    from quant_research.validation.holdout import closed_refusal
    from quant_research.validation.splits import list_splits

    paths, store = _ctx(args)
    _out([{"split_id": s.split_id, "dataset_id": s.dataset_id, "segments": s.describe()}
          | ({"parent": p["parent_id"]} if (p := store.split_parent(s.split_id)) else {})
          | ({"holdout": "closed"} if closed_refusal(store, s.split_id) else {})
          for s in list_splits(paths)])


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


def cmd_close_holdout(args) -> None:
    from quant_research.validation.splits import SplitError, load_split

    if not args.note.strip():
        raise SystemExit("closing a holdout needs a note")
    paths, store = _ctx(args)
    try:
        load_split(paths, args.split)
    except SplitError as exc:
        raise SystemExit(str(exc)) from None
    closure = store.close_holdout(args.split, args.note)
    _out({**closure, "viewings": len(store.holdout_runs(args.split)),
          "overrides": len(store.holdout_overrides(args.split))})


def cmd_holdout(args) -> None:
    from quant_research.report.markdown import write_experiment_report
    from quant_research.validation.holdout import HoldoutError, run_holdout, split_viewing_cap

    if args.candidate:
        if args.override_reason:
            raise SystemExit("--override-reason is only for the --dataset/--split path; a candidate's holdout "
                             "follows its mandate")
        return _candidate_call(_cand_holdout)(args)
    if not (args.dataset and args.split):
        raise SystemExit("give --candidate (mandate-governed) or --dataset/--split with a frozen strategy")
    paths, store = _ctx(args)
    try:
        res, prior = run_holdout(paths, store, _request(args, paths), sandbox=_sandbox(args),
                                 max_viewings=split_viewing_cap(store, args.split),
                                 override_reason=args.override_reason)
    except HoldoutError as exc:
        raise SystemExit(str(exc)) from None
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


def _load_mandate(store: ExperimentStore, ref: str):
    from quant_research.research.mandate import Mandate

    if Path(ref).is_file():
        mandate = Mandate.model_validate_json(Path(ref).read_text())
        store.put_mandate(mandate.mandate_id(), mandate.model_dump(mode="json"))
        return mandate
    rec = store.get_mandate(ref)
    if rec is None:
        raise SystemExit(f"unknown mandate {ref} (give a mandate id or a mandate JSON file)")
    return Mandate.model_validate(rec["body"])


def cmd_mandate_template(args) -> None:
    from quant_research.controller.budget import Budget
    from quant_research.research.mandate import AcceptanceCriteria, HoldoutPolicy, Mandate, SingleValidation

    tmpl = Mandate(
        question=args.question, dataset_id=args.dataset, split_id=args.split, engine_config=_engine(args),
        policy=_policy(args), evaluation_design=SingleValidation(), primary_metric="sharpe",
        acceptance=AcceptanceCriteria(min_primary=0.5, min_dsr=0.9, min_round_trips=4, max_drawdown=-0.5,
                                      require_costs_x2=True, require_delay=True),
        budget=Budget(max_rounds=5, max_candidates=20, max_experiments=300, max_wall_seconds=4 * 3600,
                      max_trials_per_strategy=50),
        holdout=HoldoutPolicy(max_viewings=1, min_primary=0.0, min_total_return=0.0),
        notes="TEMPLATE: review every threshold before `zqr mandate create`",
    )
    _out(tmpl.model_dump(mode="json"))


def cmd_mandate_create(args) -> None:
    from pydantic import ValidationError

    from quant_research.research.mandate import Mandate

    _, store = _ctx(args)
    try:
        mandate = Mandate.model_validate_json(Path(args.file).read_text())
    except ValidationError as exc:
        raise SystemExit(f"invalid mandate:\n{exc}") from None
    new = store.put_mandate(mandate.mandate_id(), mandate.model_dump(mode="json"))
    _out({"mandate_id": mandate.mandate_id(), "created": new})


def cmd_mandate_show(args) -> None:
    _, store = _ctx(args)
    rec = store.get_mandate(args.mandate_id)
    if rec is None:
        raise SystemExit(f"unknown mandate {args.mandate_id}")
    _out(rec)


def cmd_mandate_list(args) -> None:
    _, store = _ctx(args)
    _out([{"mandate_id": r["id"], "question": r["body"]["question"], "split_id": r["body"]["split_id"],
           "design": r["body"]["evaluation_design"]["kind"]} for r in store.list_mandates()])


def cmd_research_start(args) -> None:
    from quant_research.controller.agent import CLAUDE_PASS_ENV, claude_command
    from quant_research.controller.budget import ModelPricing, pricing_preset
    from quant_research.controller.controller import ResearchController, SessionConfig

    paths, store = _ctx(args)
    mandate = _load_mandate(store, args.mandate)
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
    cfg = SessionConfig(mandate=mandate, sandbox=SandboxConfig(isolation=args.isolation),
                        allow_weak_isolation=args.allow_weak_isolation, pricing=pricing)
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


def _tool(fn):
    """Run a gateway call; refusals print as JSON errors with exit code 2 so agents can read them."""
    from quant_research.controller.controller import SessionError
    from quant_research.gateway import GatewayError

    def run(args) -> None:
        paths, store = _ctx(args)
        try:
            _out(fn(args, paths, store))
        except (GatewayError, SessionError, PermissionError) as exc:
            _out({"error": str(exc)})
            sys.exit(2)
    return run


def _gw(paths, store, args):
    from quant_research.controller.tools import gateway

    return gateway(paths, store, args.session)


def _tool_backtest(args, paths, store):
    from quant_research.controller.tools import tool_backtest

    return tool_backtest(paths, store, args.session, Path(args.spec), args.segment, args.key)


def _tool_submit(args, paths, store):
    from quant_research.controller.tools import tool_submit

    return tool_submit(paths, store, args.session, args.artifact, json.loads(args.params or "{}"), args.plan,
                       args.key)


def _json_arg(text: str) -> Any:
    return json.loads(Path(text).read_text() if Path(text).is_file() else text)


TOOL_CALLS = {
    "backtest": _tool_backtest,
    "submit": _tool_submit,
    "plan": lambda a, p, s: _gw(p, s, a).submit_plan(_json_arg(a.file), a.supersedes),
    "get": lambda a, p, s: _gw(p, s, a).get_experiment(a.experiment_id),
    "compare": lambda a, p, s: _gw(p, s, a).compare_experiments(a.experiment_ids),
    "report": lambda a, p, s: _gw(p, s, a).build_report(a.file),
    "inspect": lambda a, p, s: _gw(p, s, a).inspect_dataset(),
    "source": lambda a, p, s: _gw(p, s, a).read_source(a.source_id),
    "history": lambda a, p, s: _gw(p, s, a).history(),
}


# -- benchmark v0.2 ------------------------------------------------------------------------------------
def cmd_bench_prepare(args) -> None:
    from quant_research.bench.tasks import load_tasks
    from quant_research.bench.workspace import prepare

    paths, store = _ctx(args)
    prep = prepare(paths, store, load_tasks()[args.task], f"{args.task.lower()}-prep-{int(time.time())}")
    _out({"session_id": prep.session_id, "workspace": str(prep.workspace), "run_dir": str(prep.run_dir)})


def cmd_bench_run(args) -> None:
    from quant_research.bench.adapter import ClaudeContainerAdapter
    from quant_research.bench.runner import run_task, screen

    paths, store = _ctx(args)
    adapter = ClaudeContainerAdapter(model=args.agent_model)

    def brief(r: dict) -> dict:
        res = r["result"]
        return {"run_id": res["run_id"], "task": res["task_id"], "status": res["status"],
                "failure_class": res["failure_class"], "score": res["score"], "cost": res["cost_estimated_usd"],
                "audit_ok": res["audit"]["ok"], "faults": {f["fault"]: f["outcome"] for f in res["faults"]}}

    if args.bcmd == "run":
        out = run_task(paths, store, args.task, adapter, repeat=args.repeat, cny_per_usd=args.cny_per_usd,
                       faults=args.fault)
        _out({"run_dir": out["run_dir"], **brief(out)})
    else:
        faults: dict[str, list[str]] = {}
        for spec in filter(None, (args.faults or "").split(",")):
            task, _, fault = spec.partition(":")
            faults.setdefault(task, []).append(fault)
        out = screen(paths, store, args.tasks.split(","), adapter, max_cost_usd=args.max_cost,
                     cny_per_usd=args.cny_per_usd, repeats=args.repeats, faults=faults,
                     title=args.title or "Benchmark v0.2 screen")
        _out({"report": out["report"], "spent_estimated_usd": out["spent_estimated_usd"],
              "runs": [{"task": r["task_id"], "skipped": r["skipped"]} if "skipped" in r else brief(r)
                       for r in out["runs"]]})


def cmd_bench_grade(args) -> None:
    from quant_research.bench.ledger import load_results, regrade

    paths, store = _ctx(args)
    found = load_results(paths, [args.run])
    if not found:
        raise SystemExit(f"no v0.2 result for run {args.run}")
    _out(regrade(paths, store, found[0]))


def cmd_bench_recover(args) -> None:
    from quant_research.bench.adapter import ClaudeContainerAdapter
    from quant_research.bench.runner import recover_runs

    paths, store = _ctx(args)
    _out(recover_runs(paths, store, ClaudeContainerAdapter()))


def cmd_bench_ledger(args) -> None:
    from quant_research.bench.ledger import load_results, repeat_report, write_ledger

    paths, store = _ctx(args)
    results = load_results(paths, args.runs.split(",") if args.runs else None)
    out = write_ledger(paths, results)
    if args.repeats:
        out["repeat"] = repeat_report(paths, store, results, title=args.title or "Repeat analysis")
    _out(out)


# -- candidate registry --------------------------------------------------------------------------------
def _candidate_call(fn):
    from quant_research.research.registry import RegistryError
    from quant_research.validation.holdout import HoldoutError

    def run(args) -> None:
        paths, store = _ctx(args)
        try:
            _out(fn(args, paths, store))
        except (RegistryError, HoldoutError) as exc:
            raise SystemExit(str(exc)) from None
    return run


def _cand_list(args, paths, store):
    filters = {k: v for k, v in (("session_id", args.session), ("state", args.state)) if v}
    return [{k: c[k] for k in ("id", "state", "strategy_ref", "parameters", "mandate_id", "session_id", "plan_id")}
            for c in store.list_candidates(**filters)]


def _cand_show(args, paths, store):
    cand = store.get_candidate(args.candidate_id)
    if cand is None:
        raise SystemExit(f"unknown candidate {args.candidate_id}")
    return cand | {"events": store.candidate_events(args.candidate_id)}


def _cand_freeze(args, paths, store):
    from quant_research.research.registry import freeze

    return freeze(paths, store, args.candidate_id, args.note or "")


def _cand_transition(to: str):
    def run(args, paths, store):
        from quant_research.research.registry import transition

        return transition(store, args.candidate_id, to, "human", None, args.note or "")
    return run


def _cand_holdout(args, paths, store):
    from quant_research.report.markdown import write_experiment_report
    from quant_research.validation.holdout import run_candidate_holdout

    out = run_candidate_holdout(paths, store, args.candidate, sandbox=_sandbox(args))
    res = out["result"]
    summary = {"candidate_id": args.candidate, "state": out["candidate"]["state"], "passed": out["passed"],
               "prior_holdout_runs_on_split": out["prior"], "result": _summ(res)}
    if res.has_result:
        summary["report"] = str(write_experiment_report(paths, store, res.experiment_id))
    return summary


CANDIDATE_CALLS = {
    "list": _cand_list,
    "show": _cand_show,
    "freeze": _cand_freeze,
    "approve-paper": _cand_transition("paper_approved"),
    "reject": _cand_transition("rejected"),
    "retire": _cand_transition("retired"),
}


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
    from quant_research.data.bars import to_epoch
    from quant_research.data.snapshot import load_dataset
    from quant_research.paper.runner import run_replay
    from quant_research.validation.splits import holdout_cutoff

    paths = get_paths(args.home)
    bars, manifest = load_dataset(paths, args.dataset)
    cutoff = holdout_cutoff(paths, manifest["instrument"], manifest["granularity"])
    if cutoff is not None:
        bars = bars[to_epoch(bars["bar_open_time"]) < cutoff].reset_index(drop=True)
    if args.last_bars:
        bars = bars.tail(args.last_bars + args.warmup_bars).reset_index(drop=True)
    cfg = _paper_cfg(args, paths, manifest["instrument"], manifest["granularity"])
    workdir = paths.paper / args.name
    trader = run_replay(cfg, bars, workdir, _default_product(manifest["instrument"]),
                        warmup_bars=args.warmup_bars, paths=paths,
                        faults=args.faults.split(",") if args.faults else None)
    _out(trader.write_health() | {"orders": len(trader.orders), "workdir": str(workdir),
                                  "holdout_cutoff": None if cutoff is None else to_iso(cutoff)})


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


def cmd_paper_fleet(args) -> None:
    from quant_research.desk.presets import preset_roster
    from quant_research.desk.promotion import activate_hook
    from quant_research.paper.fleet import PaperFleet, save_roster

    paths = get_paths(args.home)
    roster = Path(args.roster) if args.roster else paths.desk / "roster.json"
    if not roster.exists():
        save_roster(roster, preset_roster())
    fleet = PaperFleet(paths, roster, _client(args), on_activate=activate_hook(paths))
    _out(fleet.run(poll_seconds=args.poll_seconds, max_iterations=args.max_iterations))


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
    ap = argparse.ArgumentParser(prog="zqr", description=__doc__)
    ap.add_argument("--home", help="runtime directory (default $ZQR_HOME or ./var)")
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
    p = sub.add_parser("extend-dataset", help="freeze earlier candles followed by a dataset's bars, copied "
                                              "verbatim (A13.4; fetch the earlier range first)")
    p.add_argument("--dataset", required=True, help="the dataset to extend backwards")
    p.add_argument("--start", required=True, help="first bar of the new dataset (UTC)")
    p.add_argument("--fill-policy", choices=["none", "ffill_flat"], default="none")
    p.set_defaults(fn=cmd_extend_dataset)
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
    p.add_argument("--like", help="take the validation and holdout windows of this split (A13.4)")
    p.set_defaults(fn=cmd_split)
    sub.add_parser("splits").set_defaults(fn=cmd_splits)
    p = sub.add_parser("split-lineage", help="name a split's parent: K and trials are inherited, a closed "
                                             "parent's holdout closes the child (A13.4; the human's decision)")
    p.add_argument("--split", required=True)
    p.add_argument("--parent", required=True)
    p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_split_lineage)

    def research_cmd(name, fn, help_, segment=True, required=True):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--dataset", required=required)
        p.add_argument("--split", required=required)
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
    p = research_cmd("holdout-test", cmd_holdout, "final test on holdout (frozen candidates only)", segment=False,
                     required=False)
    p.add_argument("--candidate", help="registry candidate id (frozen); uses its mandate's viewing limit and rule")
    p.add_argument("--override-reason", help="exceed the split's viewing limit; recorded, and marks the split's "
                   "holdout as contaminated")
    p = sub.add_parser("close-holdout", help="close a split's holdout to new viewings, permanently (the human's "
                       "decision; appendix A13.3)")
    p.add_argument("--split", required=True)
    p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_close_holdout)

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

    mandate = sub.add_parser("mandate", help="research mandates (immutable)").add_subparsers(dest="mcmd", required=True)
    p = mandate.add_parser("template", help="print a mandate JSON to edit")
    p.add_argument("--dataset", required=True)
    p.add_argument("--split", required=True)
    p.add_argument("--question", required=True)
    _add_engine(p)
    p.set_defaults(fn=cmd_mandate_template)
    p = mandate.add_parser("create")
    p.add_argument("--file", required=True)
    p.set_defaults(fn=cmd_mandate_create)
    p = mandate.add_parser("show")
    p.add_argument("mandate_id")
    p.set_defaults(fn=cmd_mandate_show)
    mandate.add_parser("list").set_defaults(fn=cmd_mandate_list)

    research = sub.add_parser("research", help="agent research sessions").add_subparsers(dest="rcmd", required=True)
    p = research.add_parser("start")
    p.add_argument("--mandate", required=True, help="mandate id or mandate JSON file")
    p.add_argument("--runner", choices=["manual", "command", "claude"], default="manual",
                   help="claude: Claude Code preset (passes ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN, measures tokens)")
    p.add_argument("--command", help="agent CLI; placeholders {task_file} {workdir} {session}")
    p.add_argument("--agent-model", help="model passed to the claude preset (default: claude's own setting)")
    p.add_argument("--agent-timeout", type=float, default=3600)
    p.add_argument("--pass-env", help="comma-separated env vars the agent needs (e.g. its own API key)")
    p.add_argument("--pricing", choices=["deepseek-flash", "deepseek-v4-pro"],
                   help="vendor price preset (CNY, cache-hit price, DeepSeek peak/off-peak schedule)")
    p.add_argument("--cny-per-usd", type=float, default=7.1, help="FX for CNY presets")
    p.add_argument("--model", help="model name for custom USD prices")
    p.add_argument("--input-price", type=float, default=0.0, help="USD per 1M uncached input tokens")
    p.add_argument("--cache-read-price", type=float, help="USD per 1M cache-hit input tokens (default: input price)")
    p.add_argument("--output-price", type=float, default=0.0, help="USD per 1M output tokens")
    p.add_argument("--allow-weak-isolation", action="store_true",
                   help="allow plugin candidates with subprocess isolation (trusted code only)")
    p.add_argument("--isolation", choices=["subprocess", "docker"], default="docker")
    p.set_defaults(fn=cmd_research_start)
    for name, fn in (("resume", cmd_research_resume), ("stop", cmd_research_stop)):
        p = research.add_parser(name)
        p.add_argument("session")
        p.set_defaults(fn=fn)
    p = research.add_parser("status")
    p.add_argument("session", nargs="?")
    p.set_defaults(fn=cmd_research_status)

    tool = sub.add_parser("tool", help="research tool gateway (agents)").add_subparsers(dest="tcmd", required=True)
    tool_args: dict[str, list[tuple[tuple[str, ...], dict[str, Any]]]] = {
        "backtest": [(("--spec",), {"required": True}),
                     (("--segment",), {"choices": ["dev", "validation"], "default": "dev"}), (("--key",), {})],
        "submit": [(("--artifact",), {"required": True, "help": "template name or a .py file in the workspace"}),
                   (("--params",), {"help": "JSON object"}), (("--plan",), {}),
                   (("--key",), {"required": True, "help": "idempotency key; reuse it when retrying"})],
        "plan": [(("--file",), {"required": True, "help": "plan JSON file or inline JSON"}),
                 (("--supersedes",), {})],
        "get": [(("experiment_id",), {})],
        "compare": [(("experiment_ids",), {"nargs": "+"})],
        "report": [(("--file",), {"default": "research.json"})],
        "inspect": [],
        "source": [(("source_id",), {})],
        "history": [],
    }
    for name, extra in tool_args.items():
        p = tool.add_parser(name)
        p.add_argument("--session", required=True)
        for flags, kw in extra:
            p.add_argument(*flags, **kw)
        p.set_defaults(fn=_tool(TOOL_CALLS[name]))

    bench = sub.add_parser("bench", help="benchmark v0.2 (agent container + MCP gateway + grader)").add_subparsers(
        dest="bcmd", required=True)
    p = bench.add_parser("prepare", help="create a task session and workspace without running an agent")
    p.add_argument("--task", required=True)
    p.set_defaults(fn=cmd_bench_prepare)
    for name in ("run", "screen"):
        p = bench.add_parser(name)
        if name == "run":
            p.add_argument("--task", required=True)
            p.add_argument("--repeat", type=int, default=1)
            p.add_argument("--fault", action="append", default=[], help="inject a fault (repeatable): "
                           "timeout_after_commit, gateway_unavailable, sandbox_crash, agent_killed, model_unreachable")
        else:
            p.add_argument("--tasks", default="B02,B03,B05,B07")
            p.add_argument("--max-cost", type=float, required=True, help="stop starting tasks past this USD estimate")
            p.add_argument("--repeats", type=int, default=1)
            p.add_argument("--faults", help="TASK:FAULT,... e.g. B02:sandbox_crash,B03:gateway_unavailable")
            p.add_argument("--title")
        p.add_argument("--agent-model", help="--model passed to Claude Code (default: its environment setting)")
        p.add_argument("--cny-per-usd", type=float, default=7.1)
        p.set_defaults(fn=cmd_bench_run)
    p = bench.add_parser("grade", help="re-grade a run from its frozen submission and compare with its result")
    p.add_argument("--run", required=True)
    p.set_defaults(fn=cmd_bench_grade)
    p = bench.add_parser("recover", help="finalise runs whose harness process died (cleanup, audit, result)")
    p.set_defaults(fn=cmd_bench_recover)
    p = bench.add_parser("ledger", help="cost/violation/failure ledger over runs; --repeats adds variance analysis")
    p.add_argument("--runs", help="comma-separated run ids (default: all v0.2 runs)")
    p.add_argument("--repeats", action="store_true")
    p.add_argument("--title")
    p.set_defaults(fn=cmd_bench_ledger)

    cand = sub.add_parser("candidate", help="candidate registry lifecycle").add_subparsers(dest="ccmd", required=True)
    p = cand.add_parser("list")
    p.add_argument("--session")
    p.add_argument("--state")
    p.set_defaults(fn=_candidate_call(CANDIDATE_CALLS["list"]))
    for name in ("show", "freeze", "approve-paper", "reject", "retire"):
        p = cand.add_parser(name)
        p.add_argument("candidate_id")
        if name != "show":
            p.add_argument("--note")
        p.set_defaults(fn=_candidate_call(CANDIDATE_CALLS[name]))

    paper = sub.add_parser("paper", help="paper trading (separate process)").add_subparsers(dest="pcmd", required=True)

    def paper_common(p):
        p.add_argument("--name", required=True)
        _add_strategy(p)
        _add_engine(p)
        p.add_argument("--max-order-notional", type=float, default=5000)
        p.add_argument("--max-daily-loss", type=float, default=0.05)
        p.add_argument("--max-orders-per-hour", type=int, default=6)
        p.add_argument("--stale-after", type=float, default=7200)

    p = paper.add_parser("replay", help="replay a snapshot through the full paper stack (stops at the earliest "
                         "protected holdout of the product)")
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
    p = paper.add_parser("fleet", help="run every enabled roster strategy in one process (hot-reloads the roster)")
    p.add_argument("--roster", help="default: ZQR_HOME/desk/roster.json (created from presets if missing)")
    p.add_argument("--poll-seconds", type=float, default=30)
    p.add_argument("--max-iterations", type=int)
    p.set_defaults(fn=cmd_paper_fleet)

    from quant_research.desk.commands import register as register_desk

    register_desk(sub)

    from quant_research.hypothesis.commands import register as register_hypothesis

    register_hypothesis(sub)

    from quant_research.campaign.commands import register as register_campaign

    register_campaign(sub)

    from quant_research.inbox.commands import register as register_inbox

    register_inbox(sub)

    from quant_research.library.commands import register as register_library

    register_library(sub)

    from quant_research.memory.commands import register as register_memory

    register_memory(sub)

    from quant_research.portfolio.commands import register as register_portfolio

    register_portfolio(sub)

    from quant_research.release import register as register_release

    register_release(sub)

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
