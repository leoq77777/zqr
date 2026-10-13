from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from quant_research.paper.models import BrokerEvent, BrokerEventType, OrderIntent


class OrderState(StrEnum):
    NEW = "new"  # persisted locally, not yet sent
    SUBMITTED = "submitted"  # sent, no acknowledgement yet
    UNKNOWN = "unknown"  # send outcome unknown (timeout); must be resolved by query before new orders
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


TERMINAL = {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED}
_LIVE = {OrderState.SUBMITTED, OrderState.UNKNOWN, OrderState.OPEN, OrderState.PARTIALLY_FILLED}
ALLOWED: dict[OrderState, set[OrderState]] = {
    OrderState.NEW: {OrderState.SUBMITTED, OrderState.UNKNOWN, OrderState.REJECTED},
    OrderState.SUBMITTED: _LIVE | TERMINAL,
    OrderState.UNKNOWN: _LIVE | TERMINAL,
    OrderState.OPEN: {OrderState.PARTIALLY_FILLED} | TERMINAL,
    OrderState.PARTIALLY_FILLED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELED},
}


class OrderError(RuntimeError):
    pass


@dataclass
class Order:
    intent: OrderIntent
    state: OrderState = OrderState.NEW
    filled_qty: Decimal = Decimal("0")
    filled_notional: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    broker_order_id: str | None = None
    reject_reason: str | None = None
    seen_events: set[str] = field(default_factory=set)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def avg_price(self) -> Decimal | None:
        return self.filled_notional / self.filled_qty if self.filled_qty else None

    def transition(self, new: OrderState) -> None:
        if new == self.state and new != OrderState.PARTIALLY_FILLED:
            return
        if new not in ALLOWED.get(self.state, set()):
            raise OrderError(f"{self.intent.client_order_id}: illegal transition {self.state} -> {new}")
        self.state = new

    def apply(self, event: BrokerEvent) -> bool:
        """Apply a broker event once; duplicates (same event_id) are ignored and return False."""
        if event.event_id in self.seen_events:
            return False
        if event.client_order_id != self.intent.client_order_id:
            raise OrderError("event for a different order")
        self.seen_events.add(event.event_id)
        self.broker_order_id = event.broker_order_id or self.broker_order_id
        if event.type == BrokerEventType.ACK:
            if self.state in (OrderState.NEW, OrderState.SUBMITTED, OrderState.UNKNOWN):
                self.transition(OrderState.OPEN)
        elif event.type == BrokerEventType.FILL:
            if self.filled_qty + event.fill_qty > self.intent.base_size:
                raise OrderError(f"{self.intent.client_order_id}: overfill")
            self.filled_qty += event.fill_qty
            self.filled_notional += event.fill_qty * event.fill_price
            self.fees += event.fee
            self.transition(OrderState.FILLED if self.filled_qty == self.intent.base_size
                            else OrderState.PARTIALLY_FILLED)
        elif event.type == BrokerEventType.REJECT:
            self.reject_reason = event.reason
            self.transition(OrderState.REJECTED)
        elif event.type == BrokerEventType.CANCEL:
            self.transition(OrderState.CANCELED)
        return True
