"""Live paper blotter: roster ledgers as a single-page panel (`zqr desk panel`)."""

from __future__ import annotations

import json
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd

from quant_research.desk.promotion import roster_path
from quant_research.paper.fleet import Roster, RosterEntry, entry_costs, load_roster
from quant_research.paper.ledger import Ledger
from quant_research.paths import Paths
from quant_research.runtime import version

PANEL_HTML = Path(__file__).with_name("panel.html")
MAX_POINTS = 800
THERMOMETER_TAILS = frozenset({"buy_and_hold", "cash"})
ROLE_ORDER = ("combined", "research", "thermometer", "baseline", "shadow", "other")


def _downsample(xs: list[list[float]], n: int = MAX_POINTS) -> list[list[float]]:
    if len(xs) <= n:
        return xs
    step = len(xs) / n
    return [xs[int(i * step)] for i in range(n - 1)] + [xs[-1]]


def _book(paths: Paths, roster: Roster, entry: RosterEntry) -> dict[str, Any] | None:
    db = paths.paper / entry.name / "paper.db"
    if not db.exists():
        return None
    ledger = Ledger(db)
    try:
        marks = ledger.marks()
        if not len(marks):
            return None
        decisions = ledger.decisions()
        fills_ev = ledger.fills()
        intents = {o.intent.client_order_id: o.intent for o in ledger.load_orders().values()}
    finally:
        ledger.close()
    fee, slip, _ = entry_costs(entry, roster)
    p0 = float(decisions[0]["price"]) if decisions else float(marks["price"].iloc[0])
    bh_den = p0 * (1 + slip / 10_000) * (1 + fee)
    equity = marks["equity"].astype(float)
    price = marks["price"].astype(float)
    ts = marks["at"].astype(float)
    bh = entry.initial_cash * price / bh_den
    last_eq, first_eq = float(equity.iloc[-1]), float(equity.iloc[0])
    last_bh, first_bh = float(bh.iloc[-1]), float(bh.iloc[0])
    fills = []
    for ev in fills_ev:
        intent = intents.get(ev.client_order_id)
        fills.append({
            "t": float(ev.ts), "side": (intent.side.value if intent else ""),
            "qty": float(ev.fill_qty), "price": float(ev.fill_price), "fee": float(ev.fee),
        })
    return {
        "name": entry.name, "product": entry.product, "source": entry.source, "shadow": entry.shadow,
        "strategy_ref": entry.strategy_ref, "initial_cash": entry.initial_cash,
        "equity": last_eq, "ret": last_eq / first_eq - 1.0, "bh_ret": last_bh / first_bh - 1.0,
        "vs_bh": (last_eq / first_eq) / (last_bh / first_bh) - 1.0 if first_bh else 0.0,
        "fills_n": len(fills), "last_bar": str(marks["bar_open"].iloc[-1]),
        "last_price": float(price.iloc[-1]), "base": float(marks["base"].iloc[-1]),
        "quote": float(marks["quote"].iloc[-1]),
        "equity_series": _downsample([[float(t), float(v)] for t, v in zip(ts, equity, strict=True)]),
        "bh_series": _downsample([[float(t), float(v)] for t, v in zip(ts, bh, strict=True)]),
        "price_series": _downsample([[float(t), float(v)] for t, v in zip(ts, price, strict=True)]),
        "fills": fills[-80:], "role": book_role(entry),
    }


def book_role(entry: RosterEntry) -> str:
    """research = official portfolio paper; thermometer = cash / buy-and-hold; baseline = other presets."""
    if entry.shadow:
        return "shadow"
    if entry.source == "portfolio":
        return "research"
    if entry.source == "preset":
        tail = entry.name.split("-", 1)[-1]
        return "thermometer" if tail in THERMOMETER_TAILS else "baseline"
    return "other"


def combine_books(books: list[dict[str, Any]], name: str = "research combined") -> dict[str, Any] | None:
    """Sum official research books into one tape row so the default view is not the preset pile."""
    if not books:
        return None
    idx = pd.Index([])
    eq_parts, bh_parts = [], []
    for b in books:
        eq = pd.Series({float(t): float(v) for t, v in b["equity_series"]})
        bh = pd.Series({float(t): float(v) for t, v in b["bh_series"]})
        eq_parts.append((b, eq))
        bh_parts.append((b, bh))
        idx = idx.union(eq.index).union(bh.index)
    idx = idx.sort_values()
    eq = sum((s.reindex(idx).ffill().fillna(b["initial_cash"]) for b, s in eq_parts), start=pd.Series(0.0, index=idx))
    bh = sum((s.reindex(idx).ffill().fillna(b["initial_cash"]) for b, s in bh_parts), start=pd.Series(0.0, index=idx))
    cash = sum(b["initial_cash"] for b in books)
    last_eq, first_eq = float(eq.iloc[-1]), float(eq.iloc[0])
    last_bh, first_bh = float(bh.iloc[-1]), float(bh.iloc[0])
    fills = sorted((f for b in books for f in b["fills"]), key=lambda f: f["t"])[-80:]
    products = sorted({b["product"] for b in books})
    return {
        "name": name, "product": "+".join(products), "source": "combined", "shadow": False,
        "strategy_ref": "combined:research", "initial_cash": cash, "role": "combined",
        "combined_of": [b["name"] for b in books],
        "equity": last_eq, "ret": last_eq / first_eq - 1.0, "bh_ret": last_bh / first_bh - 1.0,
        "vs_bh": (last_eq / first_eq) / (last_bh / first_bh) - 1.0 if first_bh else 0.0,
        "fills_n": sum(b["fills_n"] for b in books), "last_bar": max(b["last_bar"] for b in books),
        "last_price": books[-1]["last_price"], "base": sum(b["base"] for b in books),
        "quote": sum(b["quote"] for b in books),
        "equity_series": _downsample([[float(t), float(v)] for t, v in eq.items()]),
        "bh_series": _downsample([[float(t), float(v)] for t, v in bh.items()]),
        "price_series": [], "fills": fills,
    }


def _research(paths: Paths) -> dict[str, Any]:
    from quant_research.campaign import budget as camp_budget
    from quant_research.desk import research as rl
    from quant_research.desk.config import load_config

    out: dict[str, Any] = {}
    try:
        cfg = load_config(paths)
        lim = camp_budget.limits(cfg)
        today = rl.utc_day(time.time())
        rows = rl.read_ledger(paths)
        day = [r for r in rows if r.get("day") == today and r.get("topic") != rl.INBOX_TOPIC]
        out = {
            "status": rl.load_state(paths).get("status"),
            "reason": rl.load_state(paths).get("reason"),
            "today_sessions": len(day),
            "today_cost_usd": round(sum(r.get("cost_usd") or 0 for r in day), 4),
            "daily_cap_usd": lim["daily_usd"],
            "total_cost_usd": round(sum(r.get("cost_usd") or 0 for r in rows if r.get("topic") != rl.INBOX_TOPIC), 4),
            "sessions_started": rl.load_state(paths).get("sessions_started"),
        }
    except Exception:  # noqa: BLE001 - panel still shows paper if the desk files are missing
        out = {}
    try:
        from quant_research.experiments.store import ExperimentStore
        from quant_research.hypothesis.store import HypothesisStore

        store = ExperimentStore(paths.db)
        try:
            counts = Counter(r["state"] for r in HypothesisStore(store).list())
            out["hypotheses"] = dict(counts)
            out["admitted"] = counts.get("admitted", 0)
        finally:
            store.close()
    except Exception:  # noqa: BLE001
        pass
    return out


def snapshot(paths: Paths) -> dict[str, Any]:
    rp = roster_path(paths)
    roster = load_roster(rp) if rp.exists() else Roster()
    books = [b for e in roster.entries if e.enabled and (b := _book(paths, roster, e))]
    research = [b for b in books if b["role"] == "research"]
    baseline = [b for b in books if b["role"] in ("baseline", "thermometer")]
    shadows = [b for b in books if b["role"] == "shadow"]
    sleeves = [b for b in books if not b["shadow"]]
    combined = combine_books(research)
    ordered = ([combined] if combined else []) + sorted(books, key=lambda b: ROLE_ORDER.index(b["role"]))
    return {
        "generated_at": time.time(),
        "version": version(),
        "research": _research(paths),
        "books": ordered,
        "research_pnl": round(sum(b["equity"] - b["initial_cash"] for b in research), 2),
        "baseline_pnl": round(sum(b["equity"] - b["initial_cash"] for b in baseline), 2),
        "sleeves_pnl": round(sum(b["equity"] - b["initial_cash"] for b in sleeves), 2),
        "shadows_pnl": round(sum(b["equity"] - b["initial_cash"] for b in shadows), 2),
        "sleeves_n": len(sleeves),
        "shadows_n": len(shadows),
        "research_n": len(research),
    }


def serve(paths: Paths, host: str, port: int, snapshot_path: Path | None = None) -> None:
    html = PANEL_HTML.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            print(f"panel: {fmt % args}")

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                body, ctype = html, "text/html; charset=utf-8"
            elif path == "/api/snapshot":
                if snapshot_path:
                    body, ctype = snapshot_path.read_bytes(), "application/json"
                else:
                    body = json.dumps(snapshot(paths), default=str).encode()
                    ctype = "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"panel: http://{host}:{port}/  (Ctrl-C to stop)", flush=True)
    httpd.serve_forever()
