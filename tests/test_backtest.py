import numpy as np
import pandas as pd
import pytest

from quant_research.backtest.engine import CostConfig, EngineConfig, run_backtest
from quant_research.data.bars import to_utc
from quant_research.evaluation.metrics import compute_metrics
from tests.conftest import HOUR, T0


def _bars(opens, closes):
    n = len(opens)
    starts = T0 + HOUR * np.arange(n)
    return pd.DataFrame({
        "bar_open_time": to_utc(starts), "open": opens, "close": closes,
        "high": np.maximum(opens, closes), "low": np.minimum(opens, closes), "filled": False,
    })


def test_golden_hand_calculated_round_trip():
    bars = _bars([100, 110, 130, 120], [100, 120, 125, 118])
    cfg = EngineConfig(initial_cash=1000, costs=CostConfig(fee_rate=0.01, slippage_bps=100), min_trade_notional=0)
    res = run_backtest(bars, np.array([1.0, 1.0, 0.0, 0.0]), cfg)

    # Bar 1 open: buy with all cash such that notional * 1.01 = 1000, at 110 * 1.01.
    notional = 1000 / 1.01
    q = notional / 111.1
    # Bar 3 open: sell everything at 120 * 0.99, pay 1% fee.
    final = q * 118.8 * 0.99
    assert final == pytest.approx(1048.1325, abs=1e-3)

    eq = res.equity["equity"].to_numpy()
    assert eq == pytest.approx([1000, q * 120, q * 125, final])
    f = res.fills
    assert list(f["side"]) == ["buy", "sell"] and list(f["bar_index"]) == [1, 3]
    assert f["fee"].tolist() == pytest.approx([notional * 0.01, q * 118.8 * 0.01])
    assert f["slippage_cost"].tolist() == pytest.approx([q * 1.1, q * 1.2])
    assert len(res.trips) == 1 and res.trips["pnl"].item() == pytest.approx(final - 1000)
    assert res.trips["bars_held"].item() == 2


def test_signal_never_fills_on_its_own_bar():
    bars = _bars([100, 200, 300], [150, 250, 350])
    cfg = EngineConfig(initial_cash=100, costs=CostConfig(fee_rate=0, slippage_bps=0), min_trade_notional=0)
    res = run_backtest(bars, np.array([1.0, 1.0, 1.0]), cfg)
    assert res.fills["bar_index"].tolist() == [1]
    assert res.fills["price"].item() == 200  # next open, not the signal bar's close of 150
    # final-bar target is never executed
    res2 = run_backtest(bars, np.array([0.0, 0.0, 1.0]), cfg)
    assert res2.fills.empty


def test_partial_target_and_no_negative_cash():
    bars = _bars([100, 100, 100, 100], [100, 100, 100, 100])
    cfg = EngineConfig(initial_cash=1000, costs=CostConfig(fee_rate=0.001, slippage_bps=0), min_trade_notional=0)
    res = run_backtest(bars, np.array([0.5, 1.0, 1.0, 1.0]), cfg)
    assert (res.equity["cash"] >= -1e-9).all()
    assert res.equity["qty"].iloc[1] == pytest.approx(5.0)
    assert res.equity["exposure"].iloc[-1] == pytest.approx(1.0, abs=1e-6)


def test_window_scores_only_inside_window_but_uses_prior_signal():
    bars = _bars([100, 100, 110, 120], [100, 100, 110, 120])
    cfg = EngineConfig(initial_cash=1000, costs=CostConfig(fee_rate=0, slippage_bps=0), min_trade_notional=0)
    res = run_backtest(bars, np.array([0, 1.0, 1.0, 1.0]), cfg, window=(T0 + 2 * HOUR, T0 + 4 * HOUR))
    assert len(res.equity) == 2
    assert res.fills["bar_index"].tolist() == [2]  # signal from bar 1 (pre-window) fills at window start


def test_metrics_definitions():
    bars = _bars([100, 100, 110, 99], [100, 110, 99, 99])
    cfg = EngineConfig(initial_cash=100, costs=CostConfig(fee_rate=0, slippage_bps=0), min_trade_notional=0)
    res = run_backtest(bars, np.array([1.0, 1.0, 1.0, 1.0]), cfg)
    m = compute_metrics(res, 100, periods_per_year=8760)
    # buy at bar1 open (100) -> equity 100, 110, 99, 99
    rets = np.array([0, 0.1, -0.1, 0])
    assert m["total_return"] == pytest.approx(-0.01)
    assert m["max_drawdown"] == pytest.approx(-0.1)
    assert m["sharpe"] == pytest.approx(rets.mean() / rets.std(ddof=1) * np.sqrt(8760))
    assert m["n_fills"] == 1 and m["n_round_trips"] == 0
