"""Backtest-vs-paper parity on the bars a paper trader saw (design 5.9).

Rule checks, decision by decision from the trader's own state at that bar, so one difference does not cascade:
targets are recomputed from the bar store with the research code; the order side and size are re-derived with the
backtest's rebalancing rule and sizing. Differences live paper is allowed to have are counted separately: orders
clipped by the order cap, buys limited by the cash buffer, and targets changed by risk limits.

Fills are compared with the backtest's fill model: the next bar's open plus slippage, and the configured fee rate.

Path check: the backtest engine replays the window from the trader's position and cash at its first decision with
the recomputed targets; the equity gap at the last decision is split into execution (fill prices and fees against
the fill model) and sizing and timing (everything else: the allowed differences above, and any rule difference).
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

import numpy as np
import pandas as pd

from quant_research.backtest.engine import CostConfig, EngineConfig, run_backtest
from quant_research.backtest.rebalance import needs_trade
from quant_research.data.bars import to_epoch
from quant_research.paper.fleet import Roster, RosterEntry, entry_costs
from quant_research.paper.ledger import Ledger
from quant_research.paper.trader import PaperConfig
from quant_research.strategy.compute import compute_targets, strategy_weights
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.strategy.spec import StrategySpec
from quant_research.timeutil import granularity_seconds, periods_per_year

CASH_BUFFER = PaperConfig.model_fields["buy_cash_buffer"].default


def _epoch(value: Any) -> int:
    return int(to_epoch(pd.Series([pd.Timestamp(value)]))[0])


def _fills_by_order(ledger: Ledger) -> dict[str, tuple[float, float, float]]:
    """client_order_id -> (quantity, notional, fee) summed over its fills."""
    out: dict[str, tuple[float, float, float]] = {}
    for ev in ledger.fills():
        q, n, f = out.get(ev.client_order_id, (0.0, 0.0, 0.0))
        out[ev.client_order_id] = (q + float(ev.fill_qty), n + float(ev.fill_qty * ev.fill_price), f + float(ev.fee))
    return out


def rule_quantity(side: str, target: float, base: float, quote: float, price: float, fee_rate: float,
                  slip_bps: float) -> float:
    """Base quantity the backtest engine trades for this target from this state, at `price` as the reference."""
    delta = target * (quote + base * price) - base * price
    if side == "buy":
        return min(delta, quote / (1 + fee_rate)) / (price * (1 + slip_bps / 10_000))
    return base if target == 0.0 else min(base, -delta / price)


def _size_check(side: str, paper_qty: float, target: float, m: dict, fee_rate: float, slip_bps: float,
                cap: float) -> tuple[str, float]:
    """('ok' | 'order_cap' | 'cash_buffer' | 'mismatch', rule quantity)."""
    price, base, quote = m["price"], m["base"], m["quote"]
    rule = rule_quantity(side, target, base, quote, price, fee_rate, slip_bps)
    tol = max(0.01, 1e-6 * m["equity"]) / price  # one cent or 1e-6 of equity: quantity rounding, not a rule
    if abs(paper_qty - rule) <= tol:
        return "ok", rule
    if rule * price > cap and abs(paper_qty * price - cap) <= tol * price:
        return "order_cap", rule
    if side == "buy" and paper_qty < rule:
        buffered = quote / ((1 + fee_rate) * (1 + slip_bps / 10_000) * (1 + CASH_BUFFER)) / price
        if abs(paper_qty - buffered) <= tol:
            return "cash_buffer", rule
    return "mismatch", rule


def paper_parity(entry: RosterEntry, roster: Roster, ledger: Ledger, bars: pd.DataFrame, paths,
                 since: float = -math.inf, until: float = math.inf) -> dict[str, Any]:
    out: dict[str, Any] = {"checked": 0, "skipped": 0, "target_mismatches": [], "order_mismatches": [],
                           "size_mismatches": [], "allowed": {}, "fills": 0, "fill_vs_next_open_bps": None,
                           "fee_rate_error": None, "path": None}
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
    mark_at = {_epoch(r["bar_open"]): r for r in ledger.marks().to_dict("records")}
    fills = _fills_by_order(ledger)
    fee_rate, slip_bps, min_notional = entry_costs(entry, roster)
    cap = roster.risk.max_order_notional
    next_open = bars["open"].to_numpy(float)
    last_i = index[_epoch(checkable[-1]["bar_open"])]
    slips, fee_errors, allowed = [], [], Counter()
    execution_usd = 0.0
    for d in checkable:
        t = _epoch(d["bar_open"])
        i = index[t]
        recomputed = None if np.isnan(weights[i]) else float(weights[i])
        recorded = d["target"]["target_fraction"]
        if recomputed != recorded:
            out["target_mismatches"].append(f"{d['bar_open']}: recorded {recorded}, recomputed {recomputed}")
        if not d["decision"]["approved"] or d["decision"]["target_fraction"] != recorded:
            allowed["risk_adjusted"] += 1
        paper_side = d["intent"]["side"] if d.get("intent") else None
        rule_side = None
        m = mark_at.get(t)
        target = d["decision"]["target_fraction"]
        if d["decision"]["approved"] and m is not None:
            position = m["base"] * m["price"]
            if needs_trade(target, position, m["equity"], tolerance=entry.policy.band, min_notional=min_notional):
                rule_side = "buy" if target * m["equity"] > position else "sell"
        if paper_side != rule_side:
            out["order_mismatches"].append(f"{d['bar_open']}: paper {paper_side or 'no order'}, backtest rule "
                                           f"{rule_side or 'no order'}")
        elif paper_side is not None and m is not None:
            paper_qty = float(d["intent"]["base_size"])
            verdict, rule = _size_check(paper_side, paper_qty, target, m, fee_rate, slip_bps,
                                        d.get("max_order_notional", cap))
            if verdict == "mismatch":
                out["size_mismatches"].append(f"{d['bar_open']}: paper {paper_side} {paper_qty:.8f}, backtest rule "
                                              f"{rule:.8f}")
            elif verdict != "ok":
                allowed[verdict] += 1
        cid = d["intent"]["client_order_id"] if d.get("intent") else None
        if cid in fills and i + 1 < len(next_open):
            qty, notional, fee = fills[cid]
            sign = 1 if paper_side == "buy" else -1
            ref = next_open[i + 1] * (1 + sign * slip_bps / 10_000)
            slips.append((notional / qty / ref - 1) * 10_000 * sign)
            fee_errors.append(abs(fee / notional - fee_rate))
            if i < last_i:
                model = qty * ref * (1 + sign * fee_rate)
                execution_usd += (model - (notional + fee)) if sign > 0 else ((notional - fee) - model)
    out["checked"] = len(checkable)
    out["fills"] = len(slips)
    out["allowed"] = dict(allowed)
    if slips:
        out["fill_vs_next_open_bps"] = float(np.mean(slips))
        out["fee_rate_error"] = float(max(fee_errors))
    out["path"] = _path(entry, upto, weights, opens, index[_epoch(checkable[0]["bar_open"])], last_i, mark_at,
                        fee_rate, slip_bps, min_notional, execution_usd)
    return out


def _path(entry: RosterEntry, upto: pd.DataFrame, weights: np.ndarray, opens: np.ndarray, first: int, last: int,
          mark_at: dict, fee_rate: float, slip_bps: float, min_notional: float,
          execution_usd: float) -> dict[str, Any] | None:
    m0, m1 = mark_at.get(int(opens[first])), mark_at.get(int(opens[last]))
    if last <= first or m0 is None or m1 is None or m0["equity"] <= 0 or m1["equity"] <= 0:
        return None
    cfg = EngineConfig(costs=CostConfig(fee_rate=fee_rate, slippage_bps=slip_bps), min_trade_notional=min_notional,
                       rebalance_tolerance=entry.policy.band)
    res = run_backtest(upto, weights, cfg, window=(int(opens[first + 1]), int(opens[last]) + 1),
                       initial=(m0["quote"], m0["base"]))
    end = res.equity.iloc[-1]
    gap = m1["equity"] - float(end["equity"])
    bps = 10_000 / m0["equity"]
    return {"bars": last - first, "equity_gap_bps": gap * bps, "execution_bps": execution_usd * bps,
            "sizing_timing_bps": (gap - execution_usd) * bps,
            "exposure_gap_pp": (m1["base"] - float(end["qty"])) * m1["price"] / m1["equity"] * 100}


def portfolio_parity(entry: RosterEntry, roster: Roster, ledger: Ledger, bars: pd.DataFrame, version, paths,
                     since: float = -math.inf, until: float = math.inf) -> dict[str, Any]:
    """Parity of a paper portfolio trader (design 5.9), with the same keys as `paper_parity`.

    Per decision: the blend recomputed from the bar store against the recorded one (target mismatches); the
    decider re-run from the recorded `pre_state` with the marked equity and position against the recorded order
    target, and the order side under the zero-tolerance rebalancing rule (order mismatches); the size as for
    strategies. Path: `run_portfolio` from the first decision's account and decider state.
    """
    from quant_research.portfolio.backtest import make_decider, run_portfolio
    from quant_research.portfolio.blend import blend
    from quant_research.portfolio.decide import DeciderState

    out: dict[str, Any] = {"checked": 0, "skipped": 0, "target_mismatches": [], "order_mismatches": [],
                           "size_mismatches": [], "allowed": {}, "fills": 0, "fill_vs_next_open_bps": None,
                           "fee_rate_error": None, "path": None}
    decisions = [d for d in ledger.decisions()
                 if since <= d["at"] < until and d.get("history_start") and d.get("portfolio")]
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
    try:
        bl = blend(version, upto, paths)
    except Exception as exc:  # noqa: BLE001
        return out | {"checked": len(checkable),
                      "target_mismatches": [f"recompute failed ({type(exc).__name__}: {exc})"]}
    index = {int(t): i for i, t in enumerate(opens)}
    mark_at = {_epoch(r["bar_open"]): r for r in ledger.marks().to_dict("records")}
    fills = _fills_by_order(ledger)
    fee_rate, slip_bps, min_notional = entry_costs(entry, roster)
    next_open = bars["open"].to_numpy(float)
    last_i = index[_epoch(checkable[-1]["bar_open"])]
    slips, fee_errors, allowed = [], [], Counter()
    execution_usd = 0.0
    for d in checkable:
        t = _epoch(d["bar_open"])
        i = index[t]
        pf = d["portfolio"]
        if abs(float(bl.target[i]) - pf["blend"]) > 1e-9:
            out["target_mismatches"].append(f"{d['bar_open']}: recorded blend {pf['blend']}, recomputed "
                                            f"{float(bl.target[i])}")
        m = mark_at.get(t)
        if m is None:
            continue
        position = m["base"] * m["price"]
        rerun = make_decider(version, DeciderState.from_dict(pf["pre_state"])).decide(pf["blend"], m["equity"],
                                                                                      position)
        rerun = None if math.isnan(rerun) else rerun
        recorded = pf["order_target"]
        if (rerun is None) != (recorded is None) or (rerun is not None and abs(rerun - recorded) > 1e-12):
            out["order_mismatches"].append(f"{d['bar_open']}: recorded order target {recorded}, decider {rerun}")
            continue
        if recorded is None:
            continue
        if not d["decision"]["approved"] or d["decision"]["target_fraction"] != recorded:
            allowed["risk_adjusted"] += 1
        target = d["decision"]["target_fraction"]
        paper_side = d["intent"]["side"] if d.get("intent") else None
        rule_side = None
        if d["decision"]["approved"] and needs_trade(target, position, m["equity"], tolerance=0.0,
                                                     min_notional=min_notional):
            rule_side = "buy" if target * m["equity"] > position else "sell"
        if paper_side != rule_side:
            out["order_mismatches"].append(f"{d['bar_open']}: paper {paper_side or 'no order'}, backtest rule "
                                           f"{rule_side or 'no order'}")
        elif paper_side is not None:
            paper_qty = float(d["intent"]["base_size"])
            verdict, rule = _size_check(paper_side, paper_qty, target, m, fee_rate, slip_bps,
                                        d.get("max_order_notional", roster.risk.max_order_notional))
            if verdict == "mismatch":
                out["size_mismatches"].append(f"{d['bar_open']}: paper {paper_side} {paper_qty:.8f}, backtest rule "
                                              f"{rule:.8f}")
            elif verdict != "ok":
                allowed[verdict] += 1
        cid = d["intent"]["client_order_id"] if d.get("intent") else None
        if cid in fills and i + 1 < len(next_open):
            qty, notional, fee = fills[cid]
            sign = 1 if paper_side == "buy" else -1
            ref = next_open[i + 1] * (1 + sign * slip_bps / 10_000)
            slips.append((notional / qty / ref - 1) * 10_000 * sign)
            fee_errors.append(abs(fee / notional - fee_rate))
            if i < last_i:
                model = qty * ref * (1 + sign * fee_rate)
                execution_usd += (model - (notional + fee)) if sign > 0 else ((notional - fee) - model)
    out["checked"] = len(checkable)
    out["fills"] = len(slips)
    out["allowed"] = dict(allowed)
    if slips:
        out["fill_vs_next_open_bps"] = float(np.mean(slips))
        out["fee_rate_error"] = float(max(fee_errors))
    first = index[_epoch(checkable[0]["bar_open"])]
    m0, m1 = mark_at.get(int(opens[first])), mark_at.get(int(opens[last_i]))
    if last_i > first and m0 is not None and m1 is not None and m0["equity"] > 0 and m1["equity"] > 0:
        run = run_portfolio(version, upto, (int(opens[first + 1]), int(opens[last_i]) + 1), paths,
                            initial=(m0["quote"], m0["base"]),
                            state=DeciderState.from_dict(checkable[0]["portfolio"]["pre_state"]), bl=bl)
        end = run.result.equity.iloc[-1]
        gap = m1["equity"] - float(end["equity"])
        bps = 10_000 / m0["equity"]
        out["path"] = {"bars": last_i - first, "equity_gap_bps": gap * bps, "execution_bps": execution_usd * bps,
                       "sizing_timing_bps": (gap - execution_usd) * bps,
                       "exposure_gap_pp": (m1["base"] - float(end["qty"])) * m1["price"] / m1["equity"] * 100}
    return out
