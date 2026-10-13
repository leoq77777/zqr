"""Fixed metric definitions. Annualisation uses the bar frequency on a 365-day, 24/7 calendar."""

from __future__ import annotations

import math

import numpy as np

from quant_research.backtest.engine import BacktestResult

METRIC_KEYS = [
    "total_return", "cagr", "ann_volatility", "sharpe", "sortino", "max_drawdown", "calmar",
    "n_fills", "n_round_trips", "win_rate", "turnover", "exposure", "total_fees", "total_slippage",
    "final_equity", "liquidation_equity", "bars", "periods_per_year",
]


def _clean(x: float) -> float:
    return 0.0 if x is None or not math.isfinite(x) else float(x)


def compute_metrics(result: BacktestResult, initial_cash: float, periods_per_year: float) -> dict[str, float]:
    equity = result.equity["equity"].to_numpy(float)
    n = len(equity)
    series = np.concatenate([[initial_cash], equity])
    rets = series[1:] / series[:-1] - 1.0
    final = float(equity[-1])
    total_return = final / initial_cash - 1.0
    years = n / periods_per_year
    cagr = (final / initial_cash) ** (1.0 / years) - 1.0 if final > 0 and years > 0 else -1.0
    std = rets.std(ddof=1) if n > 1 else 0.0
    ann_vol = std * math.sqrt(periods_per_year)
    sharpe = rets.mean() / std * math.sqrt(periods_per_year) if std > 0 else 0.0
    downside = np.minimum(rets, 0.0)
    dd_std = math.sqrt((downside**2).mean()) if n else 0.0
    sortino = rets.mean() / dd_std * math.sqrt(periods_per_year) if dd_std > 0 else 0.0
    peak = np.maximum.accumulate(series)
    max_dd = float((series / peak - 1.0).min())
    calmar = cagr / abs(max_dd) if max_dd < 0 else 0.0
    fills, trips = result.fills, result.trips
    traded = float(fills["notional"].sum()) if len(fills) else 0.0
    return {
        "total_return": _clean(total_return),
        "cagr": _clean(cagr),
        "ann_volatility": _clean(ann_vol),
        "sharpe": _clean(sharpe),
        "sortino": _clean(sortino),
        "max_drawdown": _clean(max_dd),
        "calmar": _clean(calmar),
        "n_fills": int(len(fills)),
        "n_round_trips": int(len(trips)),
        "win_rate": _clean(float((trips["pnl"] > 0).mean())) if len(trips) else 0.0,
        "turnover": _clean(traded / float(np.mean(series))),
        "exposure": _clean(float(result.equity["exposure"].mean())),
        "total_fees": _clean(float(fills["fee"].sum())) if len(fills) else 0.0,
        "total_slippage": _clean(float(fills["slippage_cost"].sum())) if len(fills) else 0.0,
        "final_equity": _clean(final),
        "liquidation_equity": _clean(result.meta.get("liquidation_equity", final)),
        "bars": n,
        "periods_per_year": periods_per_year,
    }
