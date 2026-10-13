from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def D(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class ProductSpec(BaseModel):
    product_id: str
    base_currency: str
    quote_currency: str
    base_increment: Decimal
    quote_increment: Decimal
    base_min_size: Decimal
    base_max_size: Decimal
    min_market_funds: Decimal = Decimal("1")
    status: str = "online"
    trading_disabled: bool = False

    def quantize_base(self, qty: Decimal) -> Decimal:
        return (qty / self.base_increment).to_integral_value(rounding=ROUND_DOWN) * self.base_increment

    @classmethod
    def from_coinbase(cls, body: dict[str, Any]) -> ProductSpec:
        return cls(
            product_id=body["product_id"],
            base_currency=body.get("base_currency_id") or body["product_id"].split("-")[0],
            quote_currency=body.get("quote_currency_id") or body["product_id"].split("-")[1],
            base_increment=D(body["base_increment"]),
            quote_increment=D(body["quote_increment"]),
            base_min_size=D(body["base_min_size"]),
            base_max_size=D(body["base_max_size"]),
            min_market_funds=D(body.get("quote_min_size") or "1"),
            status=body.get("status", "online"),
            trading_disabled=bool(body.get("trading_disabled", False)),
        )


class TargetPortfolio(BaseModel):
    instrument: str
    target_fraction: float
    decided_at: float  # bar close time the decision is based on
    candidate_hash: str


class RiskDecision(BaseModel):
    approved: bool
    target_fraction: float
    reduce_only: bool = False
    reasons: list[str] = Field(default_factory=list)


class OrderIntent(BaseModel):
    client_order_id: str
    instrument: str
    side: Side
    base_size: Decimal
    order_type: str = "market"
    created_at: float
    reference_price: Decimal
    reason: str = ""


class BrokerEventType(StrEnum):
    ACK = "ack"
    FILL = "fill"
    REJECT = "reject"
    CANCEL = "cancel"


class BrokerEvent(BaseModel):
    event_id: str
    client_order_id: str
    type: BrokerEventType
    broker_order_id: str | None = None
    fill_qty: Decimal = Decimal("0")
    fill_price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    ts: float
    reason: str | None = None
