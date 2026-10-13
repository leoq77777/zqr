"""Durable paper-trading ledger: orders (write-ahead), broker events, balances derived from fills."""

from __future__ import annotations

import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant_research.paper.models import BrokerEvent, BrokerEventType, OrderIntent, Side
from quant_research.paper.orders import Order, OrderState

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY, intent TEXT NOT NULL, state TEXT NOT NULL, created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL,
    recorded_at REAL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, data TEXT);
"""


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- kv ----------------------------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, default=str)))

    # -- orders ------------------------------------------------------------------------------------
    def insert_order(self, order: Order) -> None:
        self._conn.execute("INSERT INTO orders VALUES (?,?,?,?,?)",
                           (order.intent.client_order_id, order.intent.model_dump_json(), order.state,
                            order.intent.created_at, time.time()))

    def save_state(self, order: Order) -> None:
        self._conn.execute("UPDATE orders SET state=?, updated_at=? WHERE client_order_id=?",
                           (order.state, time.time(), order.intent.client_order_id))

    def record_event(self, event: BrokerEvent) -> bool:
        cur = self._conn.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
                                 (event.event_id, event.client_order_id, event.type, event.model_dump_json(),
                                  time.time()))
        return cur.rowcount == 1

    def load_orders(self) -> dict[str, Order]:
        orders: dict[str, Order] = {}
        for cid, intent_json, state in self._conn.execute(
                "SELECT client_order_id, intent, state FROM orders ORDER BY created_at"):
            orders[cid] = Order(intent=OrderIntent.model_validate_json(intent_json))
            orders[cid].state = OrderState(state) if state in (OrderState.NEW, OrderState.SUBMITTED,
                                                               OrderState.UNKNOWN) else OrderState.SUBMITTED
        for cid, data in self._conn.execute("SELECT client_order_id, data FROM events ORDER BY rowid"):
            if cid in orders:
                orders[cid].apply(BrokerEvent.model_validate_json(data))
        return orders

    def fills(self) -> list[BrokerEvent]:
        rows = self._conn.execute("SELECT data FROM events WHERE type=? ORDER BY rowid", (BrokerEventType.FILL,))
        return [BrokerEvent.model_validate_json(r[0]) for r in rows]

    def balances(self, base: str, quote: str) -> dict[str, Decimal]:
        """Initial balances plus the effect of every recorded fill."""
        initial = self.get("initial_balances", {})
        bal = {base: Decimal(initial.get(base, "0")), quote: Decimal(initial.get(quote, "0"))}
        sides = {cid: Side(json.loads(intent)["side"])
                 for cid, intent in self._conn.execute("SELECT client_order_id, intent FROM orders")}
        for ev in self.fills():
            notional = ev.fill_qty * ev.fill_price
            if sides[ev.client_order_id] == Side.BUY:
                bal[base] += ev.fill_qty
                bal[quote] -= notional + ev.fee
            else:
                bal[base] -= ev.fill_qty
                bal[quote] += notional - ev.fee
        return bal

    def orders_since(self, ts: float) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM orders WHERE created_at >= ?", (ts,)).fetchone()[0]

    def record_decision(self, at: float, data: dict[str, Any]) -> None:
        self._conn.execute("INSERT INTO decisions(at, data) VALUES (?, ?)", (at, json.dumps(data, default=str)))
