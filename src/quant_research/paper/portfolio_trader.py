"""Paper trading of a portfolio version (design 5.9): the blend of its members on the trader's history, then the
same `Decider` as the backtest (deadband and drawdown ladder) on the bar-close equity and position.

The decider's state is the ledger key `portfolio_state`, saved after every change, so a restart continues where it
stopped. Every bar gets a decision record with a `portfolio` block, orders or not, so parity can re-check each one
from its `pre_state`. A liquidated portfolio waits for a human resume: `zqr portfolio resume` writes `resume.json`
in the trader's directory, applied once at the next bar and logged as an ALERT.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any

from quant_research.backtest.rebalance import needs_trade
from quant_research.paper.models import OrderIntent, TargetPortfolio
from quant_research.paper.orders import OrderState
from quant_research.paper.trader import PaperTrader
from quant_research.portfolio.backtest import make_decider
from quant_research.portfolio.blend import blend
from quant_research.portfolio.decide import DeciderState
from quant_research.portfolio.version import PortfolioVersion
from quant_research.strategy.spec import StrategySpec

STATE_KEY = "portfolio_state"
RESUME_FILE = "resume.json"


class PortfolioTrader(PaperTrader):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.version = PortfolioVersion.model_validate(self.cfg.portfolio)
        self.version_id = self.version.version_id()
        self.decider = make_decider(self.version, DeciderState.from_dict(self.ledger.get(STATE_KEY)))
        self._last: dict[str, Any] | None = None

    def _strategy_spec(self) -> StrategySpec | None:
        return None

    def _save(self) -> None:
        self.ledger.put(STATE_KEY, self.decider.state.to_dict())

    def _account(self, price: Decimal) -> tuple[float, float]:
        b = self.balances()
        position = b[self.product.base_currency] * price
        return float(b[self.product.quote_currency] + position), float(position)

    def ingest(self, bar: dict[str, Any]) -> bool:
        if not super().ingest(bar):
            return False
        self.decider.catch_up(self._account(Decimal(str(bar["close"])))[0])
        self._save()
        return True

    def _apply_resume(self, equity: float) -> None:
        f = self.workdir / RESUME_FILE
        if not f.exists():
            return
        request = json.loads(f.read_text())
        before = self.decider.state.to_dict()
        self.decider.resume(equity)
        self._save()
        f.rename(self.workdir / f"resume.applied-{int(self.clock())}.json")
        self._log("ALERT", "portfolio_resumed", note=request.get("note"), requested_at=request.get("at"),
                  before=before, peak=equity)

    def _target(self, bar: dict[str, Any], avail: float) -> TargetPortfolio | None:
        equity, position = self._account(Decimal(str(bar["close"])))
        self._apply_resume(equity)
        bl = blend(self.version, self.history, self.paths)
        i = len(self.history) - 1
        pre = self.decider.state.to_dict()
        t = self.decider.decide(float(bl.target[i]), equity, position)
        self._save()
        s = self.decider.state
        self._last = {"version_id": self.version_id, "blend": float(bl.target[i]),
                      "pre_overlay": float(bl.pre_overlay[i]), "overlay": float(bl.overlay[i]),
                      "family_targets": {f: float(w[i]) for f, w in bl.families.items()},
                      "level": s.level, "yellow": s.yellow, "peak": s.peak, "drawdown": s.drawdown,
                      "pre_state": pre, "account": {"equity": equity, "position": position},
                      "order_target": None if math.isnan(t) else t}
        if math.isnan(t):
            return None
        return TargetPortfolio(instrument=self.cfg.instrument, target_fraction=t, decided_at=avail,
                               candidate_hash=self.version_id)

    def _no_order(self, bar: dict[str, Any], close: Decimal) -> dict[str, Any]:
        record = {"bar_open": str(bar["bar_open_time"]), "history_start": str(self.history["bar_open_time"].iloc[0]),
                  "price": str(close), "equity": str(self.equity(close)),
                  "max_order_notional": self.cfg.risk.max_order_notional, "target": None, "decision": None,
                  "intent": None, **self._record_extra()}
        self.ledger.record_decision(self.clock(), record)
        self.write_health()
        return record

    def _record_extra(self) -> dict[str, Any]:
        return {"portfolio": self._last}

    def _order_gate(self, target: float, current_value: float, equity: float) -> bool:
        return needs_trade(target, current_value, equity, tolerance=0.0, min_notional=self.cfg.min_trade_notional)

    def _send(self, intent: OrderIntent) -> None:
        super()._send(intent)
        if self.orders[intent.client_order_id].state != OrderState.REJECTED:
            self.decider.executed(self._last["order_target"])
            self._save()

    def write_health(self) -> dict[str, Any]:
        health = super().write_health()
        s = self.decider.state
        health["portfolio"] = {"version_id": self.version_id, "level": s.level, "yellow": s.yellow,
                               "drawdown": s.drawdown, "peak": s.peak, "last_target": s.last_target,
                               "resume_pending": (self.workdir / RESUME_FILE).exists()}
        (self.workdir / "health.json").write_text(json.dumps(health, indent=2, default=str))
        return health
