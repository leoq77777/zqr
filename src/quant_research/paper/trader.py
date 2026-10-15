"""StrategySnapshot -> TargetPortfolio -> RiskDecision -> OrderIntent -> BrokerEvent."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from quant_research.backtest.rebalance import needs_trade
from quant_research.data.bars import to_epoch
from quant_research.hashing import hash_obj
from quant_research.paper.broker import Broker, BrokerTimeout
from quant_research.paper.ledger import Ledger
from quant_research.paper.models import (
    BrokerEvent,
    BrokerEventType,
    D,
    OrderIntent,
    ProductSpec,
    RiskDecision,
    Side,
    TargetPortfolio,
)
from quant_research.paper.orders import Order, OrderState
from quant_research.paper.risk import RiskContext, RiskLimits, RiskManager
from quant_research.portfolio.policy import PortfolioPolicy
from quant_research.strategy.compute import compute_targets, strategy_weights
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.strategy.spec import StrategySpec
from quant_research.timeutil import granularity_seconds, periods_per_year

log = logging.getLogger(__name__)


def target_fraction(spec: StrategySpec, policy: PortfolioPolicy, history: pd.DataFrame, granularity: str,
                    paths=None, sandbox: SandboxConfig | None = None) -> float | None:
    """The policy-adjusted target for the last bar of `history`; None on an unscheduled rebalance bar."""
    tr = compute_targets(spec, history, paths, sandbox or SandboxConfig())
    targets, _ = strategy_weights(tr, policy, history, granularity_seconds(granularity), periods_per_year(granularity))
    return None if pd.isna(targets[-1]) else float(targets[-1])


def _close_epoch(bar: dict[str, Any]) -> float:
    return float(to_epoch(pd.Series([bar["bar_close_time"]]))[0])


class PaperConfig(BaseModel):
    name: str
    instrument: str
    granularity: str
    strategy_ref: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    policy: PortfolioPolicy = Field(default_factory=PortfolioPolicy)  # must equal the researched candidate's
    initial_quote: Decimal = Decimal("10000")
    initial_base: Decimal = Decimal("0")
    history_bars: int | None = None  # None keeps every bar since the feed's anchor (stateful strategies need it)
    buy_cash_buffer: float = 0.01  # headroom for price moves between decision and fill
    min_trade_notional: float = 1.0  # the engine's default; skip smaller rebalances
    reconcile_tolerance: Decimal = Decimal("0.00000001")
    unknown_query_limit: int = 3  # queries returning "not found" before an UNKNOWN order is closed
    risk: RiskLimits = Field(default_factory=RiskLimits)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    fee_rate: Decimal = Decimal("0.006")  # simulator only
    slippage_bps: Decimal = Decimal("5")  # simulator only
    portfolio: dict[str, Any] | None = None  # a portfolio version's body; traded by `PortfolioTrader`

    def identity_hash(self) -> str:
        """What is traded and how it is costed; a change needs a new strategy name. Risk limits are excluded."""
        body = self.model_dump(mode="json", exclude={"risk", "sandbox"})
        if body["portfolio"] is None:  # hashes of ledgers written before portfolios existed stay valid
            del body["portfolio"]
        return hash_obj(body)[:12]


class PaperTrader:
    def __init__(self, cfg: PaperConfig, ledger: Ledger, broker: Broker, product: ProductSpec,
                 workdir: Path, paths=None, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.ledger = ledger
        self.broker = broker
        self.product = product
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.paths = paths
        self.clock = clock
        self.risk = RiskManager(self._effective(cfg.risk))
        self.spec = self._strategy_spec()
        self.history = pd.DataFrame()
        self.last_price: Decimal | None = None
        self.halted_reason: str | None = self.ledger.get("halted_reason")
        self._not_found: dict[str, int] = {}
        if self.ledger.get("initial_balances") is None:
            self.ledger.put("initial_balances", {product.base_currency: str(cfg.initial_base),
                                                 product.quote_currency: str(cfg.initial_quote)})
        self.orders = self.ledger.load_orders()

    def _strategy_spec(self) -> StrategySpec | None:
        return StrategySpec(strategy_ref=self.cfg.strategy_ref, parameters=self.cfg.parameters).normalized()

    # -- logging / health ------------------------------------------------------------------------
    def _log(self, level: str, event: str, **data: Any) -> None:
        line = {"ts": self.clock(), "level": level, "event": event, **data}
        with open(self.workdir / "events.jsonl", "a") as f:
            f.write(json.dumps(line, default=str) + "\n")
        getattr(log, "warning" if level in ("WARN", "ALERT") else "info")("%s %s", event, data)

    def balances(self) -> dict[str, Decimal]:
        return self.ledger.balances(self.product.base_currency, self.product.quote_currency)

    def equity(self, price: Decimal) -> Decimal:
        b = self.balances()
        return b[self.product.quote_currency] + b[self.product.base_currency] * price

    def live_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if not o.terminal]

    def _effective(self, limits: RiskLimits) -> RiskLimits:
        """Data is stale only after two bar intervals at least: a daily bar is a day old just before the next."""
        floor = 2 * granularity_seconds(self.cfg.granularity)
        return limits.model_copy(update={"stale_after_seconds": max(limits.stale_after_seconds, floor)})

    def write_health(self) -> dict[str, Any]:
        now = self.clock()
        last_avail = self.ledger.get("last_data_time")
        stale_after = self.risk.limits.stale_after_seconds
        stale = last_avail is None or now - last_avail > stale_after
        health = {
            "ts": now, "name": self.cfg.name, "config_hash": self.cfg.identity_hash(),
            "risk": self.cfg.risk.model_dump(), "stale_after_effective": stale_after,
            "last_bar_open": self.ledger.get("last_bar_open"),
            "data_age_seconds": None if last_avail is None else now - last_avail,
            "last_seen": self.ledger.get("last_seen_time"), "stale": stale,
            "halted_reason": self.halted_reason, "kill_switch": self.risk.kill_switch_on(),
            "live_orders": len(self.live_orders()),
            "unknown_orders": sum(o.state == OrderState.UNKNOWN for o in self.orders.values()),
            "balances": {k: str(v) for k, v in self.balances().items()},
            "equity": str(self.equity(self.last_price)) if self.last_price else None,
        }
        (self.workdir / "health.json").write_text(json.dumps(health, indent=2, default=str))
        if stale:
            self._log("ALERT", "stale_market_data", age=health["data_age_seconds"])
        return health

    # -- event handling --------------------------------------------------------------------------
    def _apply(self, events: list[BrokerEvent]) -> None:
        for ev in events:
            order = self.orders.get(ev.client_order_id)
            if order is None:
                self._log("ALERT", "event_for_unknown_order", event=ev.model_dump())
                continue
            if not self.ledger.record_event(ev):
                self._log("INFO", "duplicate_event_ignored", event_id=ev.event_id)
                continue
            order.apply(ev)
            self.ledger.save_state(order)
            self._log("INFO", "order_event", client_order_id=ev.client_order_id, type=ev.type, state=order.state,
                      qty=ev.fill_qty, price=ev.fill_price, fee=ev.fee, reason=ev.reason)

    def _local_reject(self, order: Order, reason: str) -> None:
        self._apply([BrokerEvent(event_id=f"local-{uuid.uuid4().hex}", client_order_id=order.intent.client_order_id,
                                 type=BrokerEventType.REJECT, ts=self.clock(), reason=reason)])

    def process_market(self, price: Decimal, ts: float) -> None:
        """Feed a market price to a simulated broker so pending orders can fill."""
        self.last_price = D(price)
        on_market = getattr(self.broker, "on_market", None)
        if on_market is not None:
            self._apply(on_market(D(price), ts))

    def resolve_orders(self) -> None:
        """Query every non-terminal order by client_order_id; never resend blindly."""
        for order in self.live_orders():
            cid = order.intent.client_order_id
            if order.state == OrderState.NEW:
                # Crashed between write-ahead and send: the order was never sent.
                self._local_reject(order, "never sent (recovered after restart)")
                continue
            events = self.broker.query(cid)
            if events is None:
                self._not_found[cid] = self._not_found.get(cid, 0) + 1
                if self._not_found[cid] >= self.cfg.unknown_query_limit:
                    self._local_reject(order, f"not found at broker after {self._not_found[cid]} queries")
                else:
                    self._log("WARN", "order_not_found_yet", client_order_id=cid, attempts=self._not_found[cid])
                continue
            self._apply(events)

    def reconcile(self) -> list[str]:
        broker_bal = self.broker.balances()
        mismatches = []
        for ccy, ours in self.balances().items():
            theirs = D(broker_bal.get(ccy, 0))
            if abs(theirs - ours) > self.cfg.reconcile_tolerance:
                mismatches.append(f"{ccy}: ledger {ours} vs broker {theirs}")
        if mismatches and not self.live_orders():
            self.halt("reconciliation mismatch: " + "; ".join(mismatches))
        return mismatches

    def halt(self, reason: str) -> None:
        self.halted_reason = reason
        self.ledger.put("halted_reason", reason)
        self._log("ALERT", "halted", reason=reason)

    def recover(self) -> None:
        self.resolve_orders()
        self.reconcile()
        self._log("INFO", "recovered", live_orders=len(self.live_orders()))

    def update_risk(self, limits: RiskLimits) -> None:
        self.cfg = self.cfg.model_copy(update={"risk": limits})
        self.risk = RiskManager(self._effective(limits))
        self._log("INFO", "risk_limits_updated", limits=limits.model_dump())

    # -- bar processing --------------------------------------------------------------------------
    def _append_bar(self, bar: dict[str, Any]) -> bool:
        row = pd.DataFrame([bar])
        if not self.history.empty:
            last = to_epoch(self.history["bar_open_time"])[-1]
            if to_epoch(row["bar_open_time"])[0] <= last:
                return False
        self.history = row if self.history.empty else pd.concat([self.history, row], ignore_index=True)
        if self.cfg.history_bars:
            self.history = self.history.tail(self.cfg.history_bars).reset_index(drop=True)
        return True

    def warm_up(self, bars: pd.DataFrame) -> None:
        """Load history without deciding; bars at or before the last known bar are ignored."""
        if not len(bars):
            return
        new = bars
        if not self.history.empty:
            new = bars[to_epoch(bars["bar_open_time"]) > to_epoch(self.history["bar_open_time"])[-1]]
        if len(new):
            self.history = (new.reset_index(drop=True) if self.history.empty
                            else pd.concat([self.history, new], ignore_index=True))
            if self.cfg.history_bars:
                self.history = self.history.tail(self.cfg.history_bars).reset_index(drop=True)
        close = float(to_epoch(bars["bar_close_time"])[-1])
        if close > (self.ledger.get("last_data_time") or 0):
            self.ledger.put("last_data_time", close)

    def ingest(self, bar: dict[str, Any]) -> bool:
        """Add a bar without deciding: catch-up bars after an outage rebuild state but never trade."""
        if not self._append_bar(bar):
            return False
        self.ledger.record_bar(bar)
        self.ledger.put("last_bar_open", str(bar["bar_open_time"]))
        self.ledger.put("last_data_time", _close_epoch(bar))
        self._mark(bar, D(bar["close"]))
        return True

    def _mark(self, bar: dict[str, Any], price: Decimal) -> None:
        b = self.balances()
        self.ledger.record_mark(str(bar["bar_open_time"]), _close_epoch(bar), price,
                                b[self.product.base_currency], b[self.product.quote_currency])

    def on_bar(self, bar: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one completed bar: resolve orders, reconcile, decide, risk-check, send."""
        if not self._append_bar(bar):
            self._log("INFO", "duplicate_or_old_bar_ignored", bar_open=str(bar["bar_open_time"]))
            return None
        self.ledger.record_bar(bar)
        now = self.clock()
        avail = to_epoch(pd.Series([bar["available_at"]]))[0]
        self.ledger.put("last_bar_open", str(bar["bar_open_time"]))
        self.ledger.put("last_data_time", _close_epoch(bar))
        self.ledger.put("last_seen_time", float(avail))
        close = D(bar["close"])
        self.last_price = close
        self.resolve_orders()
        self.reconcile()
        self._mark(bar, close)

        target = self._target(bar, float(avail))
        if target is None:
            return self._no_order(bar, close)
        bal = self.balances()
        equity = self.equity(close)
        current_frac = float(bal[self.product.base_currency] * close / equity) if equity > 0 else 0.0
        day = datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%d")
        if self.ledger.get("day") != day:
            self.ledger.put("day", day)
            self.ledger.put("day_start_equity", str(equity))
        ctx = RiskContext(now=now, last_data_time=_close_epoch(bar), equity=float(equity),
                          current_fraction=current_frac, day_start_equity=float(self.ledger.get("day_start_equity")),
                          live_orders=len(self.live_orders()), orders_last_hour=self.ledger.orders_since(now - 3600),
                          halted_reason=self.halted_reason)
        decision = self.risk.evaluate(target, ctx)
        intent = self._size(decision, close, equity, bal, now) if decision.approved else None
        record = {"bar_open": str(bar["bar_open_time"]), "history_start": str(self.history["bar_open_time"].iloc[0]),
                  "price": str(close), "equity": str(equity),
                  "max_order_notional": self.cfg.risk.max_order_notional, "target": target.model_dump(),
                  "decision": decision.model_dump(), "intent": intent.model_dump() if intent else None,
                  **self._record_extra()}
        self.ledger.record_decision(now, record)
        if decision.reasons:
            self._log("WARN" if not decision.approved else "INFO", "risk_decision", **decision.model_dump())
        if intent:
            self._send(intent)
        self.write_health()
        return record

    def _target(self, bar: dict[str, Any], avail: float) -> TargetPortfolio | None:
        """The target at this bar's close; None for no new instruction (an unscheduled rebalance bar)."""
        fraction = target_fraction(self.spec, self.cfg.policy, self.history, self.cfg.granularity, self.paths,
                                   self.cfg.sandbox)
        if fraction is None:
            return None
        return TargetPortfolio(instrument=self.cfg.instrument, target_fraction=fraction, decided_at=avail,
                               candidate_hash=self.spec.candidate_hash())

    def _no_order(self, bar: dict[str, Any], close: Decimal) -> dict[str, Any] | None:
        return None

    def _record_extra(self) -> dict[str, Any]:
        return {}

    def _order_gate(self, target: float, current_value: float, equity: float) -> bool:
        return needs_trade(target, current_value, equity, tolerance=self.cfg.policy.band,
                           min_notional=self.cfg.min_trade_notional)

    def _size(self, decision: RiskDecision, price: Decimal, equity: Decimal, bal: dict[str, Decimal],
              now: float) -> OrderIntent | None:
        p = self.product
        current_value = bal[p.base_currency] * price
        if not self._order_gate(decision.target_fraction, float(current_value), float(equity)):
            return None
        delta = D(decision.target_fraction) * equity - current_value
        if decision.reduce_only and delta > 0:
            return None
        max_notional = D(self.cfg.risk.max_order_notional)
        if delta > 0:
            slip = 1 + self.cfg.slippage_bps / 10_000
            fee_room = (1 + self.cfg.fee_rate) * slip * (1 + D(self.cfg.buy_cash_buffer))
            # The backtest buys delta's worth at the slipped fill price: quantity delta / (price * (1 + slippage)).
            notional = min(delta / slip, bal[p.quote_currency] / fee_room, max_notional)
            side = Side.BUY
        else:
            notional = min(-delta, max_notional)
            side = Side.SELL
        qty = p.quantize_base(notional / price)
        if side == Side.SELL:
            if decision.target_fraction == 0.0 and -delta <= max_notional:
                qty = p.quantize_base(bal[p.base_currency])
            qty = min(qty, p.quantize_base(bal[p.base_currency]))
        if qty < p.base_min_size or qty * price < p.min_market_funds:
            return None
        return OrderIntent(client_order_id=f"qr-{uuid.uuid4().hex[:20]}", instrument=self.cfg.instrument, side=side,
                           base_size=qty, created_at=now, reference_price=price,
                           reason=f"target {decision.target_fraction:.4f}")

    def _send(self, intent: OrderIntent) -> None:
        order = Order(intent=intent)
        self.orders[intent.client_order_id] = order
        self.ledger.insert_order(order)  # write-ahead before any network call
        order.transition(OrderState.SUBMITTED)
        self.ledger.save_state(order)
        try:
            events = self.broker.submit(intent)
        except BrokerTimeout as exc:
            order.transition(OrderState.UNKNOWN)
            self.ledger.save_state(order)
            self._log("ALERT", "order_state_unknown", client_order_id=intent.client_order_id, error=str(exc))
            return
        self._apply(events)
