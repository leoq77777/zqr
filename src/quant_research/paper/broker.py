"""Broker interface and a local simulated broker with fault injection.

The simulator is the paper-trading matching engine: Coinbase's sandbox only returns static responses,
so fills are simulated locally from production public prices.
"""

from __future__ import annotations

import uuid
from collections import deque
from decimal import Decimal
from typing import Any, Protocol

from quant_research.paper.models import BrokerEvent, BrokerEventType, OrderIntent, ProductSpec, Side


class BrokerTimeout(Exception):
    """The request's outcome is unknown; the order may or may not exist at the broker."""


class Broker(Protocol):
    capabilities: dict[str, Any]

    def submit(self, intent: OrderIntent) -> list[BrokerEvent]: ...

    def query(self, client_order_id: str) -> list[BrokerEvent] | None:
        """All events for the order, or None if the broker has no record of it."""

    def balances(self) -> dict[str, Decimal]: ...


class SimulatedBroker:
    """Market orders fill at the next `on_market` price with slippage and taker fee.

    Faults are consumed one per submission from `faults`: "reject", "timeout_before_accept",
    "timeout_after_accept", "partial" (half now, rest next tick), "duplicate" (events delivered twice).
    """

    capabilities = {"order_types": ["market"], "client_order_id": True, "query_by_client_order_id": True,
                    "partial_fills": True, "simulated": True}

    def __init__(self, product: ProductSpec, balances: dict[str, Decimal], fee_rate: Decimal = Decimal("0.006"),
                 slippage_bps: Decimal = Decimal("5"), faults: list[str] | None = None):
        self.product = product
        self._balances = {k: Decimal(v) for k, v in balances.items()}
        self.fee_rate = Decimal(fee_rate)
        self.slip = Decimal(slippage_bps) / Decimal(10_000)
        self.faults: deque[str] = deque(faults or [])
        self._orders: dict[str, dict[str, Any]] = {}
        self._pending: list[str] = []
        self._outbox: list[BrokerEvent] = []

    def _event(self, cid: str, type_: BrokerEventType, ts: float, **kw: Any) -> BrokerEvent:
        ev = BrokerEvent(event_id=uuid.uuid4().hex, client_order_id=cid, type=type_,
                         broker_order_id=self._orders[cid]["broker_order_id"] if cid in self._orders else None,
                         ts=ts, **kw)
        if cid in self._orders:
            self._orders[cid]["events"].append(ev)
        return ev

    def submit(self, intent: OrderIntent) -> list[BrokerEvent]:
        fault = self.faults.popleft() if self.faults else None
        if fault == "timeout_before_accept":
            raise BrokerTimeout("request timed out before reaching the broker")
        cid = intent.client_order_id
        if cid in self._orders:
            # Real venues differ on client-id idempotency; never rely on it.
            return [BrokerEvent(event_id=uuid.uuid4().hex, client_order_id=cid, type=BrokerEventType.REJECT,
                                ts=intent.created_at, reason="duplicate client_order_id")]
        self._orders[cid] = {"intent": intent, "broker_order_id": uuid.uuid4().hex, "events": [],
                             "filled": Decimal("0"), "partial": fault == "partial",
                             "duplicate": fault == "duplicate"}
        p = self.product
        if fault == "reject":
            events = [self._event(cid, BrokerEventType.REJECT, intent.created_at, reason="injected reject")]
        elif intent.base_size < p.base_min_size or intent.base_size > p.base_max_size:
            events = [self._event(cid, BrokerEventType.REJECT, intent.created_at, reason="size outside limits")]
        elif p.trading_disabled or p.status != "online":
            events = [self._event(cid, BrokerEventType.REJECT, intent.created_at, reason="trading disabled")]
        else:
            events = [self._event(cid, BrokerEventType.ACK, intent.created_at)]
            self._pending.append(cid)
        if fault == "timeout_after_accept":
            raise BrokerTimeout("response lost after the broker accepted the order")
        return events * 2 if fault == "duplicate" else events

    def on_market(self, price: Decimal, ts: float) -> list[BrokerEvent]:
        """Match pending market orders against a new price; returns the generated events."""
        out: list[BrokerEvent] = []
        still: list[str] = []
        base, quote = self.product.base_currency, self.product.quote_currency
        for cid in self._pending:
            o = self._orders[cid]
            intent: OrderIntent = o["intent"]
            remaining = intent.base_size - o["filled"]
            qty = remaining
            if o["partial"] and o["filled"] == 0:
                qty = self.product.quantize_base(remaining / 2) or remaining
            if intent.side == Side.BUY:
                px = price * (1 + self.slip)
                notional = qty * px
                fee = notional * self.fee_rate
                if notional + fee > self._balances.get(quote, Decimal(0)):
                    out.append(self._event(cid, BrokerEventType.REJECT if o["filled"] == 0 else BrokerEventType.CANCEL,
                                           ts, reason="insufficient funds"))
                    continue
                self._balances[quote] -= notional + fee
                self._balances[base] = self._balances.get(base, Decimal(0)) + qty
            else:
                px = price * (1 - self.slip)
                if qty > self._balances.get(base, Decimal(0)):
                    out.append(self._event(cid, BrokerEventType.REJECT if o["filled"] == 0 else BrokerEventType.CANCEL,
                                           ts, reason="insufficient base balance"))
                    continue
                notional = qty * px
                fee = notional * self.fee_rate
                self._balances[base] -= qty
                self._balances[quote] = self._balances.get(quote, Decimal(0)) + notional - fee
            o["filled"] += qty
            ev = self._event(cid, BrokerEventType.FILL, ts, fill_qty=qty, fill_price=px, fee=fee)
            out.extend([ev, ev] if o["duplicate"] else [ev])
            if o["filled"] < intent.base_size:
                still.append(cid)
        self._pending = still
        return out

    def query(self, client_order_id: str) -> list[BrokerEvent] | None:
        o = self._orders.get(client_order_id)
        return list(o["events"]) if o else None

    def balances(self) -> dict[str, Decimal]:
        return dict(self._balances)
