"""Dev and validation runs of a portfolio version, and the seed validation standard (appendix A6, A14).

A dev run is an experiment of kind `portfolio` and a trial on the split; a validation run (kind
`portfolio_validation`) is not a trial but is counted in `validation_uses`. Bars come from the research segments
only. Sharpe is the A4 daily Sharpe; the drawdown is also reported without the ladder.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Literal

import numpy as np
import pandas as pd

from quant_research import ENGINE_VERSION
from quant_research.backtest.engine import run_backtest
from quant_research.data.bars import to_epoch
from quant_research.data.snapshot import load_manifest
from quant_research.evaluation.metrics import compute_metrics
from quant_research.evaluation.split_stats import sharpe_daily
from quant_research.experiments.store import ExperimentStore
from quant_research.memory.returns import archive_new, daily_returns
from quant_research.paths import Paths
from quant_research.portfolio.backtest import PortfolioRun, run_portfolio
from quant_research.portfolio.version import NO_LADDER, PortfolioVersion, VersionError, VersionStore
from quant_research.timeutil import granularity_seconds, periods_per_year
from quant_research.validation.splits import load_research_segment

Segment = Literal["dev", "validation"]
KINDS = {"dev": "portfolio", "validation": "portfolio_validation"}
SEED_STANDARD = {"min_sharpe_daily": 0.5, "min_max_drawdown": -0.25}


def daily_sharpe(equity: pd.DataFrame, initial_cash: float) -> float:
    return round(sharpe_daily(daily_returns(equity, initial_cash).to_numpy()), 6)


def family_contribution(run: PortfolioRun, first: int, initial_cash: float) -> dict[str, float]:
    """Each scored bar's P&L split by the families' shares of the pre-overlay blend at the decision before it, as a
    fraction of the initial capital; P&L while the blend is 0 goes to `none`. `first` is the window's first bar."""
    bl = run.blend
    pnl = np.diff(np.concatenate([[initial_cash], run.result.equity["equity"].to_numpy(float)]))
    out: Counter = Counter()
    for k, p in enumerate(pnl):
        i = first + k - 1
        pre = bl.pre_overlay[i] if i >= 0 else 0.0
        if pre <= 0:
            out["none"] += p
            continue
        for fam, w in bl.families.items():
            out[fam] += p * w[i] / pre
    return {f: round(v / initial_cash, 6) for f, v in sorted(out.items())}


def _run(version: PortfolioVersion, bars: pd.DataFrame, window: tuple[int, int],
         paths) -> tuple[PortfolioRun, dict[str, Any]]:
    run = run_portfolio(version, bars, window, paths)
    first = int(np.searchsorted(to_epoch(bars["bar_open_time"]), window[0]))
    cash = version.engine_config.initial_cash
    ppy = periods_per_year(version.granularity)
    metrics = compute_metrics(run.result, cash, ppy)
    no_ladder = run_portfolio(version, bars, window, paths, bl=run.blend, ladder=NO_LADDER)
    bh = run_backtest(bars, np.ones(len(bars)), version.engine_config, window=window,
                      step_seconds=granularity_seconds(version.granularity))
    bh_metrics = compute_metrics(bh, cash, ppy)
    levels = Counter(run.log["level"])
    metrics |= {
        "sharpe_daily": daily_sharpe(run.result.equity, cash),
        "max_drawdown_no_ladder": compute_metrics(no_ladder.result, cash, ppy)["max_drawdown"],
        "ladder_time": {k: round(v / len(run.log), 6) for k, v in sorted(levels.items())} if len(run.log) else {},
        "yellow_time": round(float(run.log["yellow"].mean()), 6) if len(run.log) else 0.0,
        "mean_target": round(float(np.mean(run.blend.target[first:])), 6),
        "family_contribution": family_contribution(run, first, cash),
    }
    bench = {"buy_and_hold": {"sharpe_daily": daily_sharpe(bh.equity, cash),
                              "total_return": bh_metrics["total_return"],
                              "max_drawdown": bh_metrics["max_drawdown"]}}
    return run, {"metrics": metrics, "benchmarks": bench}


def _existing(store: ExperimentStore, vid: str, kind: str, split_id: str, window: tuple[int, int]) -> dict | None:
    rows = store.query("SELECT id FROM experiments WHERE candidate_hash=? AND kind=? AND split_id=? AND status="
                       "'completed' AND window_start=? AND window_end=? AND engine_version=? ORDER BY created_at",
                       (vid, kind, split_id, window[0], window[1], ENGINE_VERSION))
    return store.get_experiment(rows[0]["id"]) if rows else None


def record_run(paths: Paths, store: ExperimentStore, version: PortfolioVersion, kind: str, segment: str,
               bars: pd.DataFrame, window: tuple[int, int]) -> dict[str, Any]:
    """Run the version on `bars` scored over `window` and record it as an experiment (idempotent per kind)."""
    vid = version.version_id()
    found = _existing(store, vid, kind, version.split_id, window)
    if found:
        return found
    manifest = load_manifest(paths, version.dataset_id)
    exp_id = store.start_experiment(
        kind=kind, strategy_ref=f"portfolio:{vid}", parameters={"version_id": vid}, candidate_hash=vid,
        dataset_id=version.dataset_id, data_hash=manifest["data_hash"], split_id=version.split_id, segment=segment,
        window_start=window[0], window_end=window[1], engine_config_id=version.engine_config.config_id(),
        engine_config=version.engine_config.model_dump(), engine_version=ENGINE_VERSION)
    try:
        run, out = _run(version, bars, window, paths)
    except Exception as exc:
        store.finish_experiment(exp_id, "failed", failure_reason=f"{type(exc).__name__}: {exc}")
        raise
    res_dir = paths.results / exp_id
    res_dir.mkdir(parents=True, exist_ok=True)
    run.result.equity.to_parquet(res_dir / "equity.parquet", index=False)
    run.result.fills.to_parquet(res_dir / "fills.parquet", index=False)
    run.result.trips.to_parquet(res_dir / "trips.parquet", index=False)
    log = run.log.assign(pre_state=run.log["pre_state"].map(json.dumps))
    log.to_parquet(res_dir / "decisions.parquet", index=False)
    (res_dir / "result.json").write_text(json.dumps({"version_id": vid, "segment": segment, **out}, indent=2,
                                                    default=str))
    store.finish_experiment(exp_id, "completed", metrics=out["metrics"], benchmarks=out["benchmarks"],
                            result_dir=str(res_dir))
    return store.get_experiment(exp_id)


def run_segment(paths: Paths, store: ExperimentStore, vid: str, segment: Segment) -> dict[str, Any]:
    vs = VersionStore(store)
    version = vs.version(vid)
    data = load_research_segment(paths, version.split_id, segment)
    kind = KINDS[segment]
    before = _existing(store, vid, kind, version.split_id, data.window)
    row = record_run(paths, store, version, kind, segment, data.bars, data.window)
    if before is None:
        if segment == "dev":
            archive_new(paths, store, [row["id"]])
        else:
            vs.record_validation_use(version.split_id, vid, row["id"])
    return row


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    m = row["metrics"]
    keys = ("sharpe_daily", "total_return", "max_drawdown", "max_drawdown_no_ladder", "turnover", "total_fees",
            "n_fills", "exposure", "mean_target", "ladder_time", "yellow_time", "family_contribution")
    return {"experiment_id": row["id"], **{k: m.get(k) for k in keys}, **(row["benchmarks"] or {})}


def gate1_reference(store: ExperimentStore, product: str, split_id: str) -> str | None:
    """The version recorded as gate 1's reference `R_p` for a product on a split (D4), if any."""
    VersionStore(store)
    rows = store.query("SELECT e.version_id FROM portfolio_events e JOIN portfolio_versions v ON v.id = e.version_id "
                       "WHERE v.product=? AND v.split_id=? AND json_extract(e.evidence, '$.gate1_reference') = 1 "
                       "ORDER BY e.id LIMIT 1", (product, split_id))
    return rows[0]["version_id"] if rows else None


def run_dev(paths: Paths, store: ExperimentStore, vid: str, reference: bool = False) -> dict[str, Any]:
    """The dev run alone: a trial, archived, no validation use; the state does not change (E1, F4).

    `reference` records the version as gate 1's reference for its product and split; a second one is refused.
    """
    vs = VersionStore(store)
    row = vs.get(vid)
    if row is None:
        raise VersionError(f"unknown portfolio version {vid}")
    current = gate1_reference(store, row["product"], row["split_id"])
    if reference and current not in (None, vid):
        raise VersionError(f"{current} is already gate 1's reference for {row['product']} on {row['split_id']}")
    dev = _summary(run_segment(paths, store, vid, "dev"))
    if reference and current is None:
        vs.record(vid, "controller", {"gate1_reference": True, "dev_experiment": dev["experiment_id"]},
                  "gate 1 reference (D4)")
    return {"version_id": vid, "state": row["state"], "split_id": row["split_id"],
            "gate1_reference": gate1_reference(store, row["product"], row["split_id"]) == vid, "dev": dev}


def version_periods(paths: Paths, store: ExperimentStore, vid: str, cuts: list[int]) -> dict[str, Any]:
    """Period statistics of the version's recorded dev run (dev data only: usable in diagnostics, D6)."""
    from quant_research.evaluation.periods import period_stats
    from quant_research.memory.returns import equity_file

    version = VersionStore(store).version(vid)
    rows = store.query("SELECT id FROM experiments WHERE candidate_hash=? AND kind='portfolio' AND split_id=? AND "
                       "status='completed' ORDER BY created_at DESC LIMIT 1", (vid, version.split_id))
    if not rows:
        raise VersionError(f"{vid} has no dev run (`zqr portfolio dev {vid}`)")
    row = store.get_experiment(rows[0]["id"])
    path = equity_file(paths, row)
    if path is None:
        raise VersionError(f"equity of {row['id']} not found")
    log_path = path.parent / "decisions.parquet"
    log = pd.read_parquet(log_path, columns=["bar_open", "level", "yellow"]) if log_path.exists() else None
    return {"version_id": vid, "split_id": version.split_id, "experiment_id": row["id"],
            "periods": period_stats(pd.read_parquet(path), version.engine_config.initial_cash, cuts, log)}


def validate(paths: Paths, store: ExperimentStore, vid: str) -> dict[str, Any]:
    """draft -> validated | failed_validation on the seed standard (A6): validation Sharpe and drawdown."""
    vs = VersionStore(store)
    row = vs.get(vid)
    if row is None:
        raise VersionError(f"unknown portfolio version {vid}")
    if row["state"] != "draft":
        raise VersionError(f"{vid} is {row['state']}; only a draft is validated")
    dev = _summary(run_segment(paths, store, vid, "dev"))
    val = _summary(run_segment(paths, store, vid, "validation"))
    passed = (val["sharpe_daily"] >= SEED_STANDARD["min_sharpe_daily"]
              and val["max_drawdown"] >= SEED_STANDARD["min_max_drawdown"])
    evidence = {"dev": dev, "validation": val, "standard": SEED_STANDARD, "passed": passed}
    vs.transition(vid, "validated" if passed else "failed_validation", "controller", evidence)
    return {"version_id": vid, "state": vs.get(vid)["state"], **evidence}
