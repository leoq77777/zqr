"""The portfolio's decision step: signal deadband (appendix A8) and drawdown ladder (investment policy 3).

The only stateful part of the portfolio path; backtest and paper call the same `Decider` with the bar-close equity
and position value, and report back when an order was executed.

- An order is generated when the target differs from the last executed target by at least the band, when the
  target is 0 and a position is open (full exits always execute), or when the ladder level changed. Price drift
  alone never trades. The account starts in cash, so the last executed target starts at 0.
- Paper only: an order that does not complete its target (capped by the order limit, rejected, cancelled) leaves
  the target open, and every later bar re-issues the current target until an order completes it.
- Ladder on the drawdown from the peak bar-close equity: halved at `halve`, back to full at `recover`, liquidated
  and halted at `liquidate` until a human resumes; resume resets the peak to the current equity.
- Paper catch-up bars after an outage update the ladder without trading; a level change seen there is traded at the
  next decision.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Literal

from quant_research.portfolio.version import Ladder

Level = Literal["full", "halved", "liquidated"]


@dataclass
class DeciderState:
    last_target: float = 0.0
    peak: float | None = None
    level: Level = "full"
    yellow: bool = False
    drawdown: float = 0.0
    pending: bool = False  # a level change seen on a catch-up bar, not traded yet
    open_target: float | None = None  # paper: an order target sent but not completed (capped, rejected, unfilled)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> DeciderState:
        return cls(**d) if d else cls()


class Decider:
    def __init__(self, band: float, ladder: Ladder, min_notional: float = 1.0, state: DeciderState | None = None):
        self.band = band
        self.ladder = ladder
        self.min_notional = min_notional
        self.state = state or DeciderState()

    def scale(self) -> float:
        return {"full": 1.0, "halved": self.ladder.halved_scale, "liquidated": 0.0}[self.state.level]

    def observe(self, equity: float) -> bool:
        """Update the peak and the ladder level from a bar-close equity; True when the level changed."""
        s, lad = self.state, self.ladder
        if s.level != "liquidated":
            s.peak = float(equity if s.peak is None else max(s.peak, equity))
        s.drawdown = float(equity / s.peak - 1.0) if s.peak else 0.0
        before = s.level
        if s.level == "full":
            if s.drawdown <= lad.liquidate:
                s.level = "liquidated"
            elif s.drawdown <= lad.halve:
                s.level = "halved"
        elif s.level == "halved":
            if s.drawdown <= lad.liquidate:
                s.level = "liquidated"
            elif s.drawdown >= lad.recover:
                s.level = "full"
        s.yellow = bool(s.drawdown <= lad.yellow)
        return s.level != before

    def catch_up(self, equity: float) -> None:
        if self.observe(equity):
            self.state.pending = True

    def decide(self, blend_target: float, equity: float, position: float) -> float:
        """The order target for this bar, or NaN for no order."""
        changed = self.observe(equity) or self.state.pending
        self.state.pending = False
        t = min(max(blend_target * self.scale(), 0.0), 1.0)
        full_exit = t == 0.0 and position >= self.min_notional
        if changed or full_exit or abs(t - self.state.last_target) >= self.band - 1e-12:
            return t
        if self.state.open_target is not None:
            return t
        return math.nan

    def sent(self, target: float) -> None:
        self.state.open_target = float(target)

    def executed(self, target: float) -> None:
        self.state.last_target = float(target)
        self.state.open_target = None

    def resume(self, equity: float) -> None:
        self.state = DeciderState(last_target=self.state.last_target, peak=float(equity))
