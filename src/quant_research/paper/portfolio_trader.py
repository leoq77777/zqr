"""Paper trading of a portfolio version (design 5.9): the blend of its members on the trader's history, then the
same `Decider` as the backtest (deadband and drawdown ladder) on the bar-close equity and position.

The decider's state is the ledger key `portfolio_state`, saved after every change, so a restart continues where it
stopped. A sent order is the ledger key `portfolio_open_order` until it is terminal; only a full fill of an order
not cut by `max_order_notional` marks its target executed. Every bar gets a decision record with a `portfolio`
block, orders or not, so parity can re-check each one from its `pre_state`.

A liquidated portfolio waits for a human resume: `zqr portfolio resume` (30 days after the liquidation time, ledger
key `portfolio_liquidated_at`, with a review) writes `resume.json` in the trader's directory, re-checked and applied
once at the next bar and logged as an ALERT.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any

from quant_research.backtest.rebalance import needs_trade
from quant_research.paper.models import OrderIntent, TargetPortfolio
from quant_research.paper.orders import TERMINAL, OrderState
from quant_research.paper.risk import RiskDecision
from quant_research.paper.trader import PaperTrader, _close_epoch
from quant_research.portfolio.backtest import make_decider
from quant_research.portfolio.blend import blend
from quant_research.portfolio.decide import DeciderState
from quant_research.portfolio.version import PortfolioVersion
from quant_research.strategy.spec import StrategySpec

STATE_KEY = "portfolio_state"
OPEN_KEY = "portfolio_open_order"
LIQUIDATED_KEY = "portfolio_liquidated_at"  # bar-close time the ladder first showed this liquidation
RESUME_FILE = "resume.json"
RESUME_WAIT_SECONDS = 30 * 86_400  # investment policy 3 (P6)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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
        self._note_liquidation(bar)
        return True

    def _note_liquidation(self, bar: dict[str, Any]) -> None:
        liquidated = self.decider.state.level == "liquidated"
        if liquidated and self.ledger.get(LIQUIDATED_KEY) is None:
            self.ledger.put(LIQUIDATED_KEY, _close_epoch(bar))
        elif not liquidated and self.ledger.get(LIQUIDATED_KEY) is not None:
            self.ledger.put(LIQUIDATED_KEY, None)

    def _apply_resume(self, equity: float) -> None:
        """Re-checked at the bar: still the liquidation the request was written for, with its review attached."""
        f = self.workdir / RESUME_FILE
        if not f.exists():
            return
        request = json.loads(f.read_text())
        problem = None
        if self.decider.state.level != "liquidated":
            problem = f"level is {self.decider.state.level}"
        elif request.get("liquidated_at") != self.ledger.get(LIQUIDATED_KEY):
            problem = "written for another liquidation"
        elif not request.get("review") or request.get("review_sha256") != _sha256(request["review"]):
            problem = "review missing or altered"
        if problem:
            f.rename(self.workdir / f"resume.rejected-{int(self.clock())}.json")
            self._log("ALERT", "portfolio_resume_rejected", reason=problem, note=request.get("note"))
            return
        before = self.decider.state.to_dict()
        self.decider.resume(equity)
        self._save()
        self.ledger.put(LIQUIDATED_KEY, None)
        f.rename(self.workdir / f"resume.applied-{int(self.clock())}.json")
        self._log("ALERT", "portfolio_resumed", note=request.get("note"), requested_at=request.get("at"),
                  review=request.get("review_ref"), review_sha256=request.get("review_sha256"), before=before,
                  peak=equity)

    def _target(self, bar: dict[str, Any], avail: float) -> TargetPortfolio | None:
        equity, position = self._account(Decimal(str(bar["close"])))
        self._settle()
        self._apply_resume(equity)
        bl = blend(self.version, self.history, self.paths)
        i = len(self.history) - 1
        pre = self.decider.state.to_dict()
        t = self.decider.decide(float(bl.target[i]), equity, position)
        self._save()
        self._note_liquidation(bar)
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

    def _settle(self) -> None:
        """Close the open order once terminal: a full, uncapped fill completes its target; anything else leaves the
        target open, so this bar's decision re-issues it."""
        o = self.ledger.get(OPEN_KEY)
        if not o:
            return
        order = self.orders.get(o["client_order_id"])
        state = order.state if order else OrderState.REJECTED
        if state not in TERMINAL:
            return
        self.ledger.put(OPEN_KEY, None)
        if state == OrderState.FILLED and not o["capped"]:
            self.decider.executed(o["target"])
        else:
            self._log("WARN", "portfolio_target_incomplete", target=o["target"], client_order_id=o["client_order_id"],
                      state=state, capped=o["capped"])
        self._save()

    def _size(self, decision: RiskDecision, price: Decimal, equity: Decimal, bal: dict[str, Decimal],
              now: float) -> OrderIntent | None:
        intent = super()._size(decision, price, equity, bal, now)
        target = self._last["order_target"]
        if (intent is None and self.decider.state.open_target is not None and decision.target_fraction == target
                and not self._order_gate(target, float(bal[self.product.base_currency] * price), float(equity))):
            self.decider.executed(target)
            self._save()
        return intent

    def _send(self, intent: OrderIntent) -> None:
        target = self._last["order_target"]
        self.decider.sent(target)
        self.ledger.put(OPEN_KEY, {"client_order_id": intent.client_order_id, "target": target,
                                   "capped": self.size_capped})
        self._save()
        super()._send(intent)

    def write_health(self) -> dict[str, Any]:
        health = super().write_health()
        s = self.decider.state
        health["portfolio"] = {"version_id": self.version_id, "level": s.level, "yellow": s.yellow,
                               "drawdown": s.drawdown, "peak": s.peak, "last_target": s.last_target,
                               "open_target": s.open_target, "liquidated_at": self.ledger.get(LIQUIDATED_KEY),
                               "resume_pending": (self.workdir / RESUME_FILE).exists()}
        (self.workdir / "health.json").write_text(json.dumps(health, indent=2, default=str))
        return health
