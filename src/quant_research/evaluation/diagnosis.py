"""Performance attribution and rule-based failure diagnosis.

Regimes and monthly splits are ex-post descriptions used for diagnosis only, never as trading signals.
Thresholds are heuristics; every label carries the evidence that triggered it.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from quant_research.backtest.engine import BacktestResult

REGIME_LOOKBACK_SECONDS = 30 * 86400

SUGGESTIONS = {
    "no_gross_edge": "The signal loses money even before costs; change the hypothesis rather than tuning it.",
    "cost_dominated": "Gross edge exists but costs remove it: trade less (longer lookbacks, wider bands, "
                      "rebalance tolerance, slower exits).",
    "overtrading": "Round trips per year are high for this fee level; reduce signal flips.",
    "mostly_beta": "Returns are explained by market exposure (high beta, no alpha); compare against holding "
                   "the asset at the same average exposure.",
    "underperforms_buy_and_hold": "Risk-adjusted return is below buy-and-hold over the same window.",
    "no_signal_value": "Holding the asset at the candidate's average exposure under the same portfolio policy and "
                       "costs does at least as well; the timing signal adds nothing.",
    "regime_dependent": "Profit comes from a single market regime; check whether that regime is predictable "
                        "or the result is a regime bet.",
    "concentrated_pnl": "A few trades or months generate most of the profit; the edge may not be repeatable.",
    "unstable_over_time": "Fewer than 40% of months are profitable.",
    "too_few_trades": "Too few round trips to judge; results are statistically weak.",
    "deep_drawdown": "Drawdown deeper than 40%.",
    "not_significant": "Sharpe is not distinguishable from zero after accounting for sample size and the "
                       "number of research trials (Deflated Sharpe).",
    "dev_validation_degradation": "Validation Sharpe is less than half of dev Sharpe: likely overfit to dev.",
}


def _sharpe(r: np.ndarray, ppy: float) -> float:
    if len(r) < 2:
        return 0.0
    sd = r.std(ddof=1)
    return float(r.mean() / sd * math.sqrt(ppy)) if sd > 0 else 0.0


def _regimes(bars: pd.DataFrame, mask: np.ndarray, step: int, strat_r: np.ndarray, asset_r: np.ndarray,
             ppy: float) -> dict[str, Any]:
    lookback = max(2, REGIME_LOOKBACK_SECONDS // step)
    close = bars["close"]
    # Regimes use only bars before t; including bar t's own return would mechanically make "trend_up" profitable.
    trend = (close / close.shift(lookback) - 1).shift(1).to_numpy()[mask]
    vol = np.log(close).diff().rolling(lookback).std().shift(1).to_numpy()[mask]
    out: dict[str, Any] = {"lookback_bars": int(lookback)}
    valid_vol = vol[~np.isnan(vol)]
    vol_median = float(np.median(valid_vol)) if len(valid_vol) else float("nan")
    splits = {
        "trend_up": trend > 0, "trend_down": trend <= 0,
        "vol_high": vol > vol_median, "vol_low": vol <= vol_median,
    }
    log_s, log_a = np.log1p(strat_r), np.log1p(asset_r)
    for name, sel in splits.items():
        sel = sel & ~np.isnan(trend if name.startswith("trend") else vol)
        out[name] = {"fraction": float(sel.mean()), "strategy_log_return": float(log_s[sel].sum()),
                     "asset_log_return": float(log_a[sel].sum()), "sharpe": _sharpe(strat_r[sel], ppy)}
    return out


def attribute(
    bt: BacktestResult,
    gross: BacktestResult,
    bars: pd.DataFrame,
    mask: np.ndarray,
    initial_cash: float,
    ppy: float,
    step: int,
) -> dict[str, Any]:
    eq = bt.equity["equity"].to_numpy(float)
    strat_r = np.diff(np.concatenate([[initial_cash], eq])) / np.concatenate([[initial_cash], eq[:-1]])
    close = bars["close"].to_numpy(float)
    idx = np.flatnonzero(mask)
    prev = np.where(idx > 0, close[np.maximum(idx - 1, 0)], bars["open"].to_numpy(float)[idx])
    asset_r = close[idx] / prev - 1
    var_a = asset_r.var(ddof=1) if len(asset_r) > 1 else 0.0
    beta = float(np.cov(strat_r, asset_r, ddof=1)[0, 1] / var_a) if var_a > 0 else 0.0
    alpha = float((strat_r.mean() - beta * asset_r.mean()) * ppy)
    years = len(eq) / ppy
    net = eq[-1] / initial_cash - 1
    gross_eq = gross.equity["equity"].to_numpy(float)
    gross_ret = gross_eq[-1] / initial_cash - 1
    trips = bt.trips
    fills = bt.fills
    out: dict[str, Any] = {
        "gross_total_return": float(gross_ret),
        "net_total_return": float(net),
        "cost_drag": float(gross_ret - net),
        "gross_sharpe": _sharpe(np.diff(gross_eq) / gross_eq[:-1], ppy) if len(gross_eq) > 2 else 0.0,
        "fees_and_slippage_over_initial": float((fills["fee"].sum() + fills["slippage_cost"].sum()) / initial_cash)
        if len(fills) else 0.0,
        "beta": beta,
        "alpha_annual": alpha,
        "correlation": float(np.corrcoef(strat_r, asset_r)[0, 1]) if strat_r.std() > 0 and asset_r.std() > 0
        else 0.0,
        "avg_exposure": float(bt.equity["exposure"].mean()),
        "round_trips_per_year": float(len(trips) / years) if years > 0 else 0.0,
        "avg_bars_held": float(trips["bars_held"].mean()) if len(trips) else 0.0,
        "regimes": _regimes(bars, mask, step, strat_r, asset_r, ppy),
    }
    monthly = bt.equity.set_index("time")["equity"]
    month_end = monthly.groupby(monthly.index.strftime("%Y-%m")).last()
    month_ret = month_end / month_end.shift(1).fillna(initial_cash) - 1
    out["months"] = int(len(month_ret))
    out["positive_month_fraction"] = float((month_ret > 0).mean()) if len(month_ret) else 0.0
    total_log = math.log(eq[-1] / initial_cash) if eq[-1] > 0 else -math.inf
    if total_log > 0 and len(month_ret):
        out["best_month_share_of_log_return"] = float(np.log1p(month_ret.max()) / total_log)
    if len(trips) >= 5 and trips["pnl"].sum() > 0:
        top = trips["pnl"].nlargest(max(1, len(trips) // 10)).sum()
        out["top_decile_trips_share_of_pnl"] = float(top / trips["pnl"].sum())
    return out


def _label(labels: list[dict[str, Any]], name: str, evidence: str) -> None:
    labels.append({"label": name, "evidence": evidence, "suggestion": SUGGESTIONS[name]})


def diagnose(metrics: dict[str, Any], attribution: dict[str, Any], benchmarks: dict[str, Any]) -> list[dict[str, Any]]:
    a, m = attribution, metrics
    labels: list[dict[str, Any]] = []
    if a["gross_sharpe"] <= 0:
        _label(labels, "no_gross_edge", f"gross Sharpe {a['gross_sharpe']:.2f}, gross return "
                                        f"{a['gross_total_return']:.1%}")
    elif a["gross_total_return"] > 0 and (m["total_return"] <= 0 or a["cost_drag"] > 0.5 * a["gross_total_return"]):
        _label(labels, "cost_dominated", f"gross {a['gross_total_return']:.1%} -> net {m['total_return']:.1%} "
                                         f"(cost drag {a['cost_drag']:.1%})")
    if a["round_trips_per_year"] > 50 and a["cost_drag"] > 0.1:
        _label(labels, "overtrading", f"{a['round_trips_per_year']:.0f} round trips/year, cost drag "
                                      f"{a['cost_drag']:.1%}")
    if a["beta"] > 0.7 and a["alpha_annual"] <= 0:
        _label(labels, "mostly_beta", f"beta {a['beta']:.2f}, annual alpha {a['alpha_annual']:.1%}")
    if m["sharpe"] < benchmarks["buy_and_hold"]["sharpe"]:
        _label(labels, "underperforms_buy_and_hold",
               f"Sharpe {m['sharpe']:.2f} vs buy-and-hold {benchmarks['buy_and_hold']['sharpe']:.2f}")
    matched = benchmarks.get("policy_matched")
    if matched and m["sharpe"] <= matched["sharpe"]:
        _label(labels, "no_signal_value", f"Sharpe {m['sharpe']:.2f} vs {matched['sharpe']:.2f} for a constant "
                                          f"proposal of {matched['proposal']:.2f}")
    reg = a["regimes"]
    for x, y in (("trend_up", "trend_down"), ("vol_high", "vol_low")):
        gx, gy = reg[x]["strategy_log_return"], reg[y]["strategy_log_return"]
        if gx + gy > 0 and min(gx, gy) < 0:
            winner = x if gx > gy else y
            _label(labels, "regime_dependent", f"profit only in {winner} ({x} {gx:+.2f} log, {y} {gy:+.2f} log)")
            break
    concentration = []
    if a.get("top_decile_trips_share_of_pnl", 0) > 1.0:
        concentration.append(f"top-decile trips {a['top_decile_trips_share_of_pnl']:.0%} of net trip PnL")
    if a.get("best_month_share_of_log_return", 0) > 0.5:
        concentration.append(f"best month {a['best_month_share_of_log_return']:.0%} of total log return")
    if concentration:
        _label(labels, "concentrated_pnl", ", ".join(concentration))
    if a["months"] >= 6 and a["positive_month_fraction"] < 0.4:
        _label(labels, "unstable_over_time", f"{a['positive_month_fraction']:.0%} of {a['months']} months positive")
    if m["n_round_trips"] < 10:
        _label(labels, "too_few_trades", f"{m['n_round_trips']} round trips")
    if m["max_drawdown"] < -0.4:
        _label(labels, "deep_drawdown", f"max drawdown {m['max_drawdown']:.1%}")
    return labels


def significance_label(stats: dict[str, Any], threshold: float = 0.9) -> dict[str, Any] | None:
    if stats.get("dsr") is None or stats["dsr"] >= threshold:
        return None
    return {"label": "not_significant",
            "evidence": f"DSR {stats['dsr']:.2f} with {stats['n_trials']} trials (PSR vs 0: {stats['psr_0']:.2f})",
            "suggestion": SUGGESTIONS["not_significant"]}
