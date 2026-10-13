"""Backtest-vs-paper parity on the bars a paper trader saw.

Targets are recomputed from the bar store with the research code. Orders are re-derived decision by decision with
the backtest's rebalancing rule from the trader's own state at that bar, so one difference does not cascade. Fills
are compared with the backtest's fill model: the next bar's open plus slippage, and the configured fee rate.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from quant_research.backtest.rebalance import needs_trade
from quant_research.data.bars import to_epoch
from quant_research.paper.fleet import Roster, RosterEntry, entry_costs
from quant_research.paper.ledger import Ledger
from quant_research.strategy.compute import compute_targets, strategy_weights
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.strategy.spec import StrategySpec
from quant_research.timeutil import granularity_seconds, periods_per_year


def _epoch(value: Any) -> int:
    return int(to_epoch(pd.Series([pd.Timestamp(value)]))[0])


def _fills_by_order(ledger: Ledger) -> dict[str, tuple[float, float, float]]:
    """client_order_id -> (quantity, notional, fee) summed over its fills."""
    out: dict[str, tuple[float, float, float]] = {}
    for ev in ledger.fills():
        q, n, f = out.get(ev.client_order_id, (0.0, 0.0, 0.0))
        out[ev.client_order_id] = (q + float(ev.fill_qty), n + float(ev.fill_qty * ev.fill_price), f + float(ev.fee))
    return out


def paper_parity(entry: RosterEntry, roster: Roster, ledger: Ledger, bars: pd.DataFrame, paths,
                 since: float = -math.inf, until: float = math.inf) -> dict[str, Any]:
    out: dict[str, Any] = {"checked": 0, "skipped": 0, "target_mismatches": [], "order_mismatches": [],
                           "fills": 0, "fill_vs_next_open_bps": None, "fee_rate_error": None}
    decisions = [d for d in ledger.decisions() if since <= d["at"] < until and d.get("history_start")]
    if not decisions:
        return out | {"note": "no decisions in the window"}
    if not len(bars):
        return out | {"skipped": len(decisions), "note": "no bar store"}
    opens = to_epoch(bars["bar_open_time"])
    checkable = [d for d in decisions if _epoch(d["history_start"]) == int(opens[0])]
    out["skipped"] = len(decisions) - len(checkable)
    if not checkable:
        return out | {"note": "decisions predate the bar store"}
    upto = bars[opens <= _epoch(checkable[-1]["bar_open"])].reset_index(drop=True)
    spec = StrategySpec(strategy_ref=entry.strategy_ref, parameters=entry.parameters)
    sandbox = SandboxConfig(isolation="docker" if entry.strategy_ref.startswith("plugin:") else "subprocess")
    try:
        tr = compute_targets(spec, upto, paths, sandbox)
    except Exception as exc:  # noqa: BLE001
        return out | {"checked": len(checkable),
                      "target_mismatches": [f"recompute failed ({type(exc).__name__}: {exc})"]}
    weights, _ = strategy_weights(tr, entry.policy, upto, granularity_seconds(entry.granularity),
                                  periods_per_year(entry.granularity))
    index = {int(t): i for i, t in enumerate(opens)}
    marks = ledger.marks()
    mark_at = {_epoch(r["bar_open"]): r for r in marks.to_dict("records")}
    fills = _fills_by_order(ledger)
    fee_rate, slip_bps, min_notional = entry_costs(entry, roster)
    next_open = bars["open"].to_numpy(float)
    slips, fee_errors = [], []
    for d in checkable:
        t = _epoch(d["bar_open"])
        i = index[t]
        recomputed = None if np.isnan(weights[i]) else float(weights[i])
        recorded = d["target"]["target_fraction"]
        if recomputed != recorded:
            out["target_mismatches"].append(f"{d['bar_open']}: recorded {recorded}, recomputed {recomputed}")
        paper_side = d["intent"]["side"] if d.get("intent") else None
        rule_side = None
        m = mark_at.get(t)
        if d["decision"]["approved"] and m is not None:
            target = d["decision"]["target_fraction"]
            position = m["base"] * m["price"]
            if needs_trade(target, position, m["equity"], tolerance=entry.policy.band, min_notional=min_notional):
                rule_side = "buy" if target * m["equity"] > position else "sell"
        if paper_side != rule_side:
            out["order_mismatches"].append(f"{d['bar_open']}: paper {paper_side or 'no order'}, backtest rule "
                                           f"{rule_side or 'no order'}")
        cid = d["intent"]["client_order_id"] if d.get("intent") else None
        if cid in fills and i + 1 < len(next_open):
            qty, notional, fee = fills[cid]
            sign = 1 if paper_side == "buy" else -1
            ref = next_open[i + 1] * (1 + sign * slip_bps / 10_000)
            slips.append((notional / qty / ref - 1) * 10_000 * sign)
            fee_errors.append(abs(fee / notional - fee_rate))
    out["checked"] = len(checkable)
    out["fills"] = len(slips)
    if slips:
        out["fill_vs_next_open_bps"] = float(np.mean(slips))
        out["fee_rate_error"] = float(max(fee_errors))
    return out
