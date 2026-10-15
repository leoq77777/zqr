"""A13.8 sensitivity comparison on a copy of the production home (dev window only; writes only into the copy).

    python docs/reviews/2026-10-15-a13-8-sensitivity/sensitivity.py /tmp/zqr-a138/home > results.json

A: the A13.5 long-dev datasets (isolated prints dropped and filled flat), the engine as released.
B: the same rows rebuilt from raw under A13.8 (prints kept, isolated opens corrected), the engine guard on.
R: B's bars with the recorded values of the isolated opens put back (the rule first approved: guard only).
C: R's bars with the guard off (isolated opens filled at their recorded open).
only <time>: A's bars with that one row taken from R.

The copy also rehearses the migration: extend-dataset, create-split --like, split-lineage (revalidates).
"""

from __future__ import annotations

import json
import sys

import numpy as np

import quant_research.backtest.engine as engine
from quant_research.backtest.engine import run_backtest
from quant_research.campaign.store import CampaignStore
from quant_research.data.bars import to_epoch
from quant_research.data.coinbase import VENUE, raw_dir_for
from quant_research.data.snapshot import extend_dataset, load_dataset, load_manifest
from quant_research.data.validate import load_raw_candles, print_flags, validate_bars
from quant_research.evaluation.metrics import compute_metrics
from quant_research.experiments.store import ExperimentStore
from quant_research.memory.clusters import effective_k
from quant_research.paths import get_paths
from quant_research.portfolio.evaluate import _run, daily_sharpe
from quant_research.portfolio.version import VersionStore
from quant_research.timeutil import granularity_seconds, periods_per_year
from quant_research.validation.splits import create_split_like, link_split, load_research_segment, load_split

CASES = {
    "BTC-USD": {"version": "pv-bef1798ef4d9", "split": "split-f0d7a178e08b"},
    "ETH-USD": {"version": "pv-77bad34a29c6", "split": "split-d6a76022a19d"},
}
AFTER = 30 * 86400
OHLC = ["open", "high", "low", "close"]
KEYS = ("sharpe_daily", "total_return", "max_drawdown", "sharpe_daily_no_ladder", "total_return_no_ladder",
        "max_drawdown_no_ladder", "n_fills", "turnover", "total_fees", "ladder_time")


def _flags(bars) -> dict:
    opens, wicks = print_flags(bars.reset_index(drop=True))
    t = to_epoch(bars["bar_open_time"])
    return {"isolated_opens": [int(x) for x in t[opens]], "isolated_wicks": [int(x) for x in t[wicks]]}


def _rebuild(paths, store, split_id: str) -> tuple[str, dict]:
    """extend-dataset from the long split's base under A13.8, create-split --like, split-lineage."""
    old = load_split(paths, split_id)
    om = load_manifest(paths, old.dataset_id)
    base = load_manifest(paths, om["extends"])
    raw_dir = raw_dir_for(paths.raw, base["instrument"], base["granularity"])
    bars, rep = validate_bars(load_raw_candles(raw_dir), VENUE, base["instrument"], base["granularity"],
                              om["requested_start"], base["actual_first_open"], fill_policy=om["fill_policy"],
                              flag_prints=True)
    m = extend_dataset(paths, base["dataset_id"], bars, rep, raw_dir)
    new = create_split_like(paths, m["dataset_id"], split_id)
    link = link_split(paths, store, new.split_id, split_id, "A13.8 rehearsal: isolated prints kept, opens corrected")
    cs = CampaignStore(store)
    return new.split_id, {
        "dataset_id": m["dataset_id"], "split_id": new.split_id, "kind": link["kind"],
        "rows": (om["rows"], m["rows"]), "filled_rows": (om["filled_rows"], m["filled_rows"]),
        "isolated_prints": rep.isolated_prints, "ancestors": store.split_ancestors(new.split_id),
        "trials": store.split_trial_count(new.split_id), "K": (effective_k(store, split_id),
                                                               effective_k(store, new.split_id)),
        "quotas_old": cs.quotas(paths, split_id), "quotas_new": cs.quotas(paths, new.split_id)}


def _recorded(bars, prints: list[dict]):
    out = bars.copy()
    t = to_epoch(out["bar_open_time"])
    for p in prints:
        i = int(np.flatnonzero(t == p["start"])[0])
        out.loc[i, OHLC] = [p[k] for k in OHLC]
    return out


def _portfolio(version, bars, window, paths) -> tuple[dict, object]:
    run, _, out = _run(version, bars, window, paths)
    m = out["metrics"]
    levels = run.log["level"].tolist()
    times = to_epoch(run.log["bar_open"])
    events = [{"at": int(times[k]), "from": levels[k - 1], "to": levels[k],
               "drawdown": round(float(run.log["drawdown"].iloc[k]), 4)}
              for k in range(1, len(levels)) if levels[k] != levels[k - 1]]
    return {**{k: m.get(k) for k in KEYS}, "final_equity": round(float(run.result.equity["equity"].iloc[-1]), 2),
            "ladder_events": events, "buy_and_hold": out["benchmarks"]["buy_and_hold"]}, run


def _member(version, targets, bars, window) -> dict:
    cfg = version.engine_config
    res = run_backtest(bars, targets, cfg, window=window, step_seconds=granularity_seconds(version.granularity))
    m = compute_metrics(res, cfg.initial_cash, periods_per_year(version.granularity))
    return {"sharpe_daily": daily_sharpe(res.equity, cfg.initial_cash), "total_return": m["total_return"],
            "max_drawdown": m["max_drawdown"], "n_fills": m["n_fills"]}


def _after(run_a, run_x, bars, starts) -> dict:
    """Bars in the 30 days after each flagged bar where a member's own target differs from A's."""
    t = to_epoch(bars["bar_open_time"])
    out = {}
    for s in starts:
        sel = (t >= s) & (t < s + AFTER)
        diff = {e: int(np.sum(np.abs(run_a.blend.members[e][sel] - run_x.blend.members[e][sel]) > 1e-12))
                for e in run_a.blend.members}
        out[str(s)] = {"member_bars_differing": {e: n for e, n in diff.items() if n},
                       "blend_bars_differing": int(np.sum(np.abs(run_a.blend.target[sel] - run_x.blend.target[sel])
                                                          > 1e-12))}
    return out


def main(home: str) -> None:
    paths = get_paths(home)
    store = ExperimentStore(paths.db)
    vs = VersionStore(store)
    report: dict = {"current_datasets": {}}
    for d in sorted(p.name for p in paths.snapshots.iterdir()):
        bars, _ = load_dataset(paths, d)
        report["current_datasets"][d] = _flags(bars)
    for product, case in CASES.items():
        version = vs.version(case["version"])
        new_split, rebuild = _rebuild(paths, store, case["split"])
        a = load_research_segment(paths, case["split"], "dev")
        b = load_research_segment(paths, new_split, "dev")
        assert a.window == b.window and len(a.bars) == len(b.bars)
        r_bars = _recorded(b.bars, rebuild["isolated_prints"])
        changed = np.flatnonzero((a.bars[OHLC].to_numpy() != r_bars[OHLC].to_numpy()).any(axis=1))
        pa, run_a = _portfolio(version, a.bars, a.window, paths)
        pb, run_b = _portfolio(version, b.bars, b.window, paths)
        pr, run_r = _portfolio(version, r_bars, b.window, paths)
        guard = engine.isolated_opens
        engine.isolated_opens = lambda o, c: np.zeros(len(o), bool)
        try:
            pc, run_c = _portfolio(version, r_bars, b.window, paths)
            members_c = {e: _member(version, x, r_bars, b.window) for e, x in run_c.blend.members.items()}
        finally:
            engine.isolated_opens = guard
        recorded = store.query("SELECT metrics FROM experiments WHERE candidate_hash=? AND kind='portfolio' AND "
                               "split_id=? AND status='completed' ORDER BY created_at LIMIT 1",
                               (case["version"], case["split"]))
        only = {}
        for i in changed:
            bars = a.bars.copy()
            bars.loc[i, OHLC] = r_bars.loc[i, OHLC].to_numpy()
            only[f"only {int(to_epoch(a.bars['bar_open_time'])[i])}"] = _portfolio(version, bars, a.window, paths)[0]
        starts = [p["start"] for p in rebuild["isolated_prints"]]
        report[product] = {
            "window": a.window, "rebuild": rebuild,
            "rows_changed": [{"start": int(to_epoch(a.bars["bar_open_time"])[i]), "A": a.bars.loc[i, OHLC].tolist(),
                              "B": b.bars.loc[i, OHLC].tolist(), "R": r_bars.loc[i, OHLC].tolist()}
                             for i in changed],
            "flags_B": _flags(b.bars),
            "recorded_A": json.loads(recorded[0]["metrics"]) if recorded else None,
            "portfolio": {"A": pa, "B": pb, "R": pr, "C": pc, **only},
            "members": {e: {"A": _member(version, run_a.blend.members[e], a.bars, a.window),
                            "B": _member(version, run_b.blend.members[e], b.bars, b.window),
                            "R": _member(version, run_r.blend.members[e], r_bars, b.window),
                            "C": members_c[e]} for e in run_a.blend.members},
            "after_flagged": {"B": _after(run_a, run_b, a.bars, starts), "R": _after(run_a, run_r, a.bars, starts)},
        }
    store.close()
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1])
