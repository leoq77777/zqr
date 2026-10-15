"""Inbox items in the experiment database: the text, its provenance, the draft and the campaigns written from it.

States: `received` -> `drafting` (controller) -> `drafted` | `infeasible` | `failed`; `drafting` -> `received` when
the run failed for reasons outside the agent's work; `failed` -> `received` (human `retry`); `received` ->
`discarded` (human `discard`).
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, date, datetime
from typing import Any

from quant_research.desk.config import load_config
from quant_research.experiments.store import ExperimentStore
from quant_research.paths import Paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_items (
    id TEXT PRIMARY KEY, text TEXT NOT NULL, text_hash TEXT NOT NULL UNIQUE, url TEXT, published TEXT,
    product TEXT, note TEXT, state TEXT NOT NULL, draft TEXT, campaign_ids TEXT, reason TEXT,
    cost_usd REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS inbox_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, event TEXT NOT NULL, actor TEXT NOT NULL,
    data TEXT, at REAL NOT NULL);
"""
STATES = ("received", "drafting", "drafted", "infeasible", "failed", "discarded")
MAX_TEXT = 20_000
TRANSITIONS: dict[tuple[str, str], set[str]] = {
    ("received", "drafting"): {"controller"},
    ("drafting", "drafted"): {"controller"},
    ("drafting", "infeasible"): {"controller"},
    ("drafting", "failed"): {"controller"},
    ("drafting", "received"): {"controller"},
    ("failed", "received"): {"human"},
    ("received", "discarded"): {"human"},
}


class InboxError(ValueError):
    pass


def text_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


class InboxStore:
    def __init__(self, store: ExperimentStore):
        self.store = store
        store.ensure_schema(SCHEMA)

    def add(self, paths: Paths, text: str, url: str | None = None, published: str | None = None,
            product: str | None = None, note: str | None = None) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise InboxError("the text is empty")
        if len(text) > MAX_TEXT:
            raise InboxError(f"the text has {len(text)} characters; at most {MAX_TEXT}")
        if published is not None:
            try:
                published = date.fromisoformat(published).isoformat()
            except ValueError:
                raise InboxError(f"--published {published!r} is not YYYY-MM-DD") from None
        if product is not None and product not in load_config(paths).mandates:
            raise InboxError(f"{product} has no desk mandate ({', '.join(load_config(paths).mandates) or 'none'})")
        h = text_hash(text)
        if dup := self.store.query("SELECT id FROM inbox_items WHERE text_hash=?", (h,)):
            raise InboxError(f"the same text is already in the inbox as {dup[0]['id']}")
        iid, now = "inb-" + h[:12], time.time()
        self.store.execute("INSERT INTO inbox_items (id, text, text_hash, url, published, product, note, state, "
                           "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (iid, text, h, url, published, product, note, "received", now, now))
        self.record(iid, "received", "human", {"url": url, "published": published, "product": product, "note": note})
        return self.require(iid)

    def get(self, iid: str) -> dict[str, Any] | None:
        rows = self.store.query("SELECT * FROM inbox_items WHERE id=?", (iid,))
        return self._row(rows[0]) if rows else None

    def require(self, iid: str) -> dict[str, Any]:
        row = self.get(iid)
        if row is None:
            raise InboxError(f"unknown inbox item {iid}")
        return row

    def list(self, state: str | None = None) -> list[dict[str, Any]]:
        where, args = ("state=?", (state,)) if state else ("1=1", ())
        return [self._row(r) for r in self.store.query(
            f"SELECT * FROM inbox_items WHERE {where} ORDER BY created_at, id", args)]

    @staticmethod
    def _row(r: dict[str, Any]) -> dict[str, Any]:
        return r | {"draft": json.loads(r["draft"]) if r["draft"] else None,
                    "campaign_ids": json.loads(r["campaign_ids"]) if r["campaign_ids"] else []}

    def next_received(self) -> dict[str, Any] | None:
        rows = self.list("received")
        return rows[0] if rows else None

    def transition(self, iid: str, to: str, actor: str, note: str = "", **update: Any) -> dict[str, Any]:
        row = self.require(iid)
        allowed = TRANSITIONS.get((row["state"], to))
        if allowed is None:
            raise InboxError(f"{iid} is {row['state']}; it cannot become {to}")
        if actor not in allowed:
            raise InboxError(f"{row['state']} -> {to} is for the {' or '.join(sorted(allowed))}, not {actor}")
        if actor == "human" and not note.strip():
            raise InboxError("the human's decision needs a note")
        cols = {"state": to, "updated_at": time.time()}
        for k in ("draft", "campaign_ids"):
            if k in update:
                cols[k] = json.dumps(update.pop(k), default=str, ensure_ascii=False)
        if "reason" in update:
            cols["reason"] = update.pop("reason")
        self.store.execute(f"UPDATE inbox_items SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                           (*cols.values(), iid))
        self.record(iid, to, actor, update | ({"note": note} if note else {}))
        return self.require(iid)

    def add_cost(self, iid: str, usd: float) -> None:
        self.store.execute("UPDATE inbox_items SET cost_usd = cost_usd + ? WHERE id=?", (usd, iid))

    def record(self, iid: str, event: str, actor: str, data: dict[str, Any] | None = None) -> None:
        self.store.execute("INSERT INTO inbox_events (item_id, event, actor, data, at) VALUES (?,?,?,?,?)",
                           (iid, event, actor, json.dumps(data or {}, sort_keys=True, default=str,
                                                          ensure_ascii=False), time.time()))

    def events(self, iid: str | None = None) -> list[dict[str, Any]]:
        rows = (self.store.query("SELECT * FROM inbox_events WHERE item_id=? ORDER BY id", (iid,)) if iid
                else self.store.query("SELECT * FROM inbox_events ORDER BY id"))
        return [r | {"data": json.loads(r["data"] or "{}")} for r in rows]

    def events_on(self, day: str) -> list[dict[str, Any]]:
        return [e for e in self.events() if _day(e["at"]) == day]

    def drafts_started_on(self, day: str) -> int:
        return sum(e["event"] == "drafting" for e in self.events_on(day))
