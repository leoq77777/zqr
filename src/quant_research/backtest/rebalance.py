"""When a target becomes an order: one rule for the backtest engine and the paper trader."""

from __future__ import annotations

EPS = 1e-9  # relative to equity: below this a rebalance is floating-point noise


def needs_trade(target: float, position_value: float, equity: float, *, tolerance: float,
                min_notional: float) -> bool:
    """Trade when the target value differs from the position value by at least
    max(min_notional, tolerance * equity, EPS * equity), or when the target is 0 and a position is open."""
    if target == 0.0 and position_value > 0:
        return True
    threshold = max(min_notional, tolerance * equity, EPS * equity)
    return abs(target * equity - position_value) >= threshold
