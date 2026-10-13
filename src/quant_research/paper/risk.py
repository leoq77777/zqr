from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, NonNegativeFloat, PositiveFloat

from quant_research.paper.models import RiskDecision, TargetPortfolio


class RiskLimits(BaseModel):
    max_position_fraction: float = 1.0
    max_order_notional: PositiveFloat = 5_000.0
    max_daily_loss_fraction: PositiveFloat = 0.05
    max_orders_per_hour: int = 6
    stale_after_seconds: NonNegativeFloat = 2 * 3600
    kill_switch_file: str | None = None


@dataclass
class RiskContext:
    now: float
    last_data_time: float | None  # when the latest market data became available
    equity: float
    current_fraction: float
    day_start_equity: float
    live_orders: int  # non-terminal orders, including UNKNOWN
    orders_last_hour: int
    halted_reason: str | None = None


class RiskManager:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def kill_switch_on(self) -> bool:
        return bool(self.limits.kill_switch_file) and Path(self.limits.kill_switch_file).exists()

    def evaluate(self, target: TargetPortfolio, ctx: RiskContext) -> RiskDecision:
        lim = self.limits
        reasons: list[str] = []
        blocked = False
        if ctx.halted_reason:
            blocked = True
            reasons.append(f"halted: {ctx.halted_reason}")
        if ctx.live_orders:
            blocked = True
            reasons.append(f"{ctx.live_orders} order(s) not in a terminal state; no new orders until resolved")
        if ctx.last_data_time is None or ctx.now - ctx.last_data_time > lim.stale_after_seconds:
            blocked = True
            reasons.append("market data is stale")
        if ctx.orders_last_hour >= lim.max_orders_per_hour:
            blocked = True
            reasons.append("order rate limit reached")
        frac = min(max(target.target_fraction, 0.0), lim.max_position_fraction)
        if frac < target.target_fraction:
            reasons.append(f"target clipped to max position fraction {lim.max_position_fraction}")
        reduce_only = False
        if self.kill_switch_on():
            reduce_only = True
            reasons.append("kill switch active: reduce-only")
        if ctx.day_start_equity > 0 and ctx.equity / ctx.day_start_equity - 1 <= -lim.max_daily_loss_fraction:
            reduce_only = True
            reasons.append("daily loss limit hit: reduce-only")
        if reduce_only:
            frac = min(frac, ctx.current_fraction)
        return RiskDecision(approved=not blocked, target_fraction=frac, reduce_only=reduce_only, reasons=reasons)
