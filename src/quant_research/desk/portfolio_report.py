"""Portfolio sections of the desk reports (design 5.9): per product portfolio, the combined account, shadow
portfolios (phase 4, D2), the monthly review. Read-only: ledgers, marks, decision records, events and
`VersionStore`.

Buy and hold uses the portfolio's capital bought at its first decision price with the entry fee and slippage. The
combined account is the sum of the product portfolios with the yellow flag on its drawdown only (the ladder acts per
product, investment policy 3). Family contribution splits each day's P&L by the families' shares of the
pre-overlay blend at the last decision before the day starts.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from quant_research.desk.promotion import roster_path
from quant_research.experiments.store import ExperimentStore
from quant_research.paper.barstore import BarStore
from quant_research.paper.fleet import Roster, RosterEntry, entry_costs, load_roster
from quant_research.paper.ledger import Ledger
from quant_research.paper.parity import portfolio_parity
from quant_research.paths import Paths
from quant_research.portfolio.version import Ladder, VersionStore

COMBINED_YELLOW = Ladder().yellow


@dataclass
class Book:
    entry: RosterEntry
    marks: pd.DataFrame
    decisions: list[dict[str, Any]]
    fills: list[Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    fee: float = 0.0
    slip: float = 0.0

    @property
    def p0(self) -> float:
        return float(self.decisions[0]["price"]) if self.decisions else float(self.marks["price"].iloc[0])

    def benchmark(self, price: pd.Series) -> pd.Series:
        return self.entry.initial_cash * price / (self.p0 * (1 + self.slip / 10_000) * (1 + self.fee))


def _window(start: str, end: str) -> tuple[float, float]:
    t = [datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() for d in (start, end)]
    return t[0], t[1]


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:+.2%}"


def load_books(paths: Paths, roster: Roster | None = None, shadow: bool = False) -> list[Book]:
    """The approved portfolio books, or with `shadow` the shadow books (never both: shadows stay out of the
    combined account and the soft stop)."""
    if roster is None:
        rp = roster_path(paths)
        roster = load_roster(rp) if rp.exists() else Roster()
    books = []
    for e in sorted(roster.entries, key=lambda e: e.name):
        db = paths.paper / e.name / "paper.db"
        if e.source != "portfolio" or e.shadow != shadow or not db.exists():
            continue
        ledger = Ledger(db)
        try:
            marks = ledger.marks()
            if not len(marks):
                continue
            book = Book(e, marks, ledger.decisions(), ledger.fills())
        finally:
            ledger.close()
        ev = paths.paper / e.name / "events.jsonl"
        if ev.exists():
            for line in ev.read_text().splitlines():
                try:
                    book.events.append(json.loads(line))
                except ValueError:
                    continue
        book.fee, book.slip, _ = entry_costs(e, roster)
        books.append(book)
    return books


def combined(books: list[Book]) -> pd.DataFrame:
    """Per bar close (`at`): each book's equity and benchmark, their sums `combined` and `benchmark`, from the
    first close where every book has a mark."""
    if not books:
        return pd.DataFrame(columns=["combined", "benchmark"])
    cols = {}
    for b in books:
        m = b.marks.set_index("at")
        cols[f"equity:{b.entry.name}"] = m["equity"]
        cols[f"benchmark:{b.entry.name}"] = b.benchmark(m["price"])
    df = pd.DataFrame(cols).sort_index().ffill().dropna()
    df["combined"] = df[[c for c in df if c.startswith("equity:")]].sum(axis=1)
    df["benchmark"] = df[[c for c in df if c.startswith("benchmark:")]].sum(axis=1)
    return df


def daily(series: pd.Series) -> pd.Series:
    """The last value of each UTC day, indexed by day."""
    day = pd.to_datetime(series.index, unit="s", utc=True).normalize()
    return series.groupby(day).last()


def _value_at(series: pd.Series, t: float) -> float | None:
    upto = series[series.index <= t]
    return float(upto.iloc[-1]) if len(upto) else None


def _period_return(series: pd.Series, t0: float, t1: float) -> float | None:
    end = _value_at(series, t1)
    start = _value_at(series, t0)
    if start is None:
        after = series[series.index > t0]
        start = float(after.iloc[0]) if len(after) else None
    if end is None or start is None or series[(series.index > t0) & (series.index <= t1)].empty:
        return None
    return end / start - 1


def family_contribution(book: Book, t0: float, t1: float) -> dict[str, float]:
    """P&L in USD over [t0, t1) split day by day by the families' shares of the pre-overlay blend at the last
    decision before each day (the first decision of the day when there is none); P&L with a zero blend is `none`."""
    eq = book.marks.set_index("at")["equity"]
    decisions = [d for d in book.decisions if d.get("portfolio")]
    out: Counter = Counter()
    day = t0
    while day < t1:
        nxt = day + 86_400
        pnl = _value_at(eq, nxt)
        start = _value_at(eq, day)
        if pnl is None or eq[(eq.index > day) & (eq.index <= nxt)].empty:
            day = nxt
            continue
        pnl -= start if start is not None else book.entry.initial_cash
        prior = [d for d in decisions if d["at"] <= day]
        same_day = [d for d in decisions if day < d["at"] <= nxt]
        pick = prior[-1] if prior else (same_day[0] if same_day else None)
        pf = pick["portfolio"] if pick else None
        pre = pf["pre_overlay"] if pf else 0.0
        if pre <= 0:
            out["none"] += pnl
        else:
            for fam, w in pf["family_targets"].items():
                out[fam] += pnl * w / pre
        day = nxt
    return {k: round(v, 2) for k, v in sorted(out.items())}


def _drawdown(series: pd.Series) -> pd.Series:
    return series / series.cummax() - 1


TABLE_HEADER = ["| portfolio | version | state | equity | day | day B&H | total | total B&H | drawdown | ladder | "
                "fills | turnover | fees | parity |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]


def portfolio_section(paths: Paths, store: ExperimentStore, day: str) -> tuple[list[str], dict[str, Any]]:
    t0, t1 = _window(day, (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"))
    rp = roster_path(paths)
    roster = load_roster(rp) if rp.exists() else Roster()
    books = load_books(paths, roster)
    shadows = load_books(paths, roster, shadow=True)
    summary = {"portfolios": len(books), "shadows": len(shadows), "parity_mismatches": 0, "alerts": 0}
    vs = VersionStore(store)
    lines: list[str] = []
    if books:
        rows, details = _book_rows(paths, roster, vs, books, t0, t1, summary)
        lines += ["## Portfolios", "",
                  "Each product portfolio is one paper account (blend, deadband and drawdown ladder of its version). "
                  "Buy & hold: the same capital bought at the first decision price with entry costs.", "",
                  *TABLE_HEADER, *rows, "", *details]
        lines += _combined_lines(books, t0, t1, summary)
    if shadows:
        rows, details = _book_rows(paths, roster, vs, shadows, t0, t1, summary)
        lines += [*([""] if lines else []), "## Shadow portfolios (not approved)", "",
                  "Versions that did not pass validation or the holdout, run on paper for forward evidence only "
                  "(phase 4, D2). They are not part of the combined account or the soft stop.", "",
                  *TABLE_HEADER, *rows, "", *details]
    return lines, summary


def _book_rows(paths: Paths, roster: Roster, vs: VersionStore, books: list[Book], t0: float, t1: float,
               summary: dict[str, Any]) -> tuple[list[str], list[str]]:
    rows, details = [], []
    for b in books:
        e, m = b.entry, b.marks.set_index("at")
        vid = e.portfolio_version
        vrow = vs.get(vid) if vid else None
        ledger = Ledger(paths.paper / e.name / "paper.db")
        try:
            bars = BarStore.for_feed(paths, e.product, e.granularity).bars
            par = portfolio_parity(e, roster, ledger, bars, vs.version(vid), paths, since=t0, until=t1)
        except Exception as exc:  # noqa: BLE001 - one portfolio's parity must not sink the report
            par = {"checked": 0, "fills": 0, "order_mismatches": [], "size_mismatches": [], "allowed": {},
                   "path": None, "target_mismatches": [f"parity failed ({type(exc).__name__}: {exc})"]}
        finally:
            ledger.close()
        mismatches = par["target_mismatches"] + par["order_mismatches"] + par["size_mismatches"]
        fills = [f for f in b.fills if t0 <= f.ts < t1]
        notional = sum(float(f.fill_qty * f.fill_price) for f in fills)
        start_eq = _value_at(m["equity"], t0) or e.initial_cash
        alerts = Counter(ev["event"] for ev in b.events
                         if t0 <= ev.get("ts", 0) < t1 and ev.get("level") in ("WARN", "ALERT"))
        summary["parity_mismatches"] += len(mismatches)
        summary["alerts"] += sum(alerts.values())
        end = _value_at(m["equity"], t1)
        bench = b.benchmark(m["price"])
        dd = _drawdown(m["equity"][m.index <= t1])
        last = next((d["portfolio"] for d in reversed(b.decisions) if d.get("portfolio") and d["at"] < t1), None)
        s = last or {}
        rows.append(f"| {e.name} | {vid} | {vrow['state'] if vrow else '-'} | "
                    f"{'-' if end is None else f'{end:,.2f}'} | {_pct(_period_return(m['equity'], t0, t1))} | "
                    f"{_pct(_period_return(m['price'], t0, t1))} | "
                    f"{_pct(None if end is None else end / e.initial_cash - 1)} | "
                    f"{_pct(_value_at(bench, t1) / e.initial_cash - 1 if _value_at(bench, t1) else None)} | "
                    f"{_pct(s.get('drawdown'))} | {s.get('level', '-')}{' ⚠ yellow' if s.get('yellow') else ''} | "
                    f"{len(fills)} | {notional / start_eq:.2f} | {sum(float(f.fee) for f in fills):.2f} | "
                    f"{par['checked'] - len(mismatches)}/{par['checked']} |")
        if last:
            fams = ", ".join(f"{k} {v:.3f}" for k, v in sorted(last["family_targets"].items()))
            last_order = next((d["portfolio"]["order_target"] for d in reversed(b.decisions)
                               if d.get("portfolio") and d["at"] < t1 and d.get("intent")), None)
            details.append(f"- {e.name}: blend {last['blend']:.3f} (pre-overlay {last['pre_overlay']:.3f} × overlay "
                           f"{last['overlay']:.3f}); families {fams}; last executed order target "
                           f"{'-' if last_order is None else f'{last_order:.3f}'}; max drawdown to date "
                           f"{dd.min():+.2%}")
        contrib = family_contribution(b, t0, t1)
        if contrib:
            details.append("  - day P&L by family (USD): " + ", ".join(f"{k} {v:+.2f}" for k, v in contrib.items()))
        if alerts or mismatches:
            details.append("  - attention: " + ", ".join(f"{k}×{v}" for k, v in alerts.items())
                           + "".join(f"\n    - parity: {x}" for x in mismatches[:5]))
        path = par.get("path")
        if path:
            details.append(f"  - vs backtest over {path['bars']} bars: equity {path['equity_gap_bps']:+.1f} bps "
                           f"(execution {path['execution_bps']:+.1f}, sizing and timing "
                           f"{path['sizing_timing_bps']:+.1f})")
    return rows, details


def _combined_lines(books: list[Book], t0: float, t1: float, summary: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    comb = combined(books)
    if len(comb):
        c, bench = comb["combined"], comb["benchmark"]
        capital = sum(b.entry.initial_cash for b in books)
        end = _value_at(c, t1)
        dd = float(_drawdown(c).iloc[-1]) if len(c[c.index <= t1]) else None
        parts = " + ".join(f"{b.entry.product} {_value_at(comb[f'equity:{b.entry.name}'], t1) or 0:,.2f}"
                           for b in books)
        lines += ["", f"Combined account: equity {'-' if end is None else f'{end:,.2f}'} ({parts}); day "
                      f"{_pct(_period_return(c, t0, t1))} vs B&H {_pct(_period_return(bench, t0, t1))}; total "
                      f"{_pct(None if end is None else end / capital - 1)} vs B&H "
                      f"{_pct((_value_at(bench, t1) or capital) / capital - 1)}; drawdown {_pct(dd)}"
                      + (" ⚠ yellow flag" if dd is not None and dd <= COMBINED_YELLOW else "")]
        summary["combined_equity"] = end
    return lines


def versions_lines(store: ExperimentStore, mandates: dict[str, str]) -> list[str]:
    """Per desk mandate's split: portfolio versions by state and validation uses."""
    vs = VersionStore(store)
    rows = []
    for product, mid in mandates.items():
        rec = store.get_mandate(mid)
        if rec is None:
            continue
        split_id = rec["body"].get("split_id")
        states = Counter(v["state"] for v in vs.list(split_id=split_id))
        rows.append(f"| {product} | {', '.join(f'{k} {v}' for k, v in sorted(states.items())) or '-'} | "
                    f"{vs.validation_uses(split_id)} |")
    if not rows:
        return []
    return ["", "| product | portfolio versions by state | validation uses |", "|---|---|---|", *rows]


def write_monthly_report(paths: Paths, store: ExperimentStore, month: str) -> Path:
    """`desk/reports/monthly-YYYY-MM.md`: the month per portfolio and combined against buy and hold, drawdowns,
    ladder events, family contribution, versions and the soft-stop status (investment policy 2)."""
    from quant_research.desk import softstop

    first = datetime.strptime(month + "-01", "%Y-%m-%d")
    nxt = (first + timedelta(days=32)).replace(day=1)
    t0, t1 = _window(first.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d"))
    books = load_books(paths)
    shadows = load_books(paths, shadow=True)
    vs = VersionStore(store)
    lines = [f"# Monthly review {month} (UTC)", "",
             f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC. Paper trading only; no live orders exist.", "",
             "| portfolio | version | state | month | month B&H | max drawdown in month | ladder events |",
             "|---|---|---|---|---|---|---|"]
    contrib_lines = []
    for b in books + shadows:
        m = b.marks.set_index("at")
        dd = _drawdown(m["equity"])
        in_month = dd[(dd.index > t0) & (dd.index <= t1)]
        events, prev = [], None
        for d in b.decisions:
            pf = d.get("portfolio")
            if not pf:
                continue
            if prev is not None and pf["level"] != prev and t0 <= d["at"] < t1:
                events.append(f"{d['bar_open'][:16]} {prev}→{pf['level']}")
            prev = pf["level"]
        events += [f"{datetime.fromtimestamp(ev['ts'], UTC):%Y-%m-%d %H:%M} resumed" for ev in b.events
                   if ev.get("event") == "portfolio_resumed" and t0 <= ev.get("ts", 0) < t1]
        vrow = vs.get(b.entry.portfolio_version) if b.entry.portfolio_version else None
        label = f"{b.entry.name} (shadow, not approved)" if b.entry.shadow else b.entry.name
        lines.append(f"| {label} | {b.entry.portfolio_version} | {vrow['state'] if vrow else '-'} | "
                     f"{_pct(_period_return(m['equity'], t0, t1))} | {_pct(_period_return(m['price'], t0, t1))} | "
                     f"{_pct(float(in_month.min()) if len(in_month) else None)} | {'; '.join(events) or '-'} |")
        contrib = family_contribution(b, t0, t1)
        if contrib:
            contrib_lines.append(f"- {label} (USD): " + ", ".join(f"{k} {v:+.2f}" for k, v in contrib.items()))
    comb = combined(books)
    if len(comb):
        dd = _drawdown(comb["combined"])
        in_month = dd[(dd.index > t0) & (dd.index <= t1)]
        worst = float(in_month.min()) if len(in_month) else None
        lines += ["", f"Combined account: month {_pct(_period_return(comb['combined'], t0, t1))} vs B&H "
                      f"{_pct(_period_return(comb['benchmark'], t0, t1))}; max drawdown in month {_pct(worst)}"
                      + (" ⚠ yellow flag" if worst is not None and worst <= COMBINED_YELLOW else "")]
    if contrib_lines:
        lines += ["", "Family contribution:", *contrib_lines]
    status = softstop.evaluate(paths, nxt.strftime("%Y-%m-%d"), books=books)
    lines += ["", f"Soft stop (investment policy 2): {status['status']}"
              + (f" — combined Sharpe {status['combined_sharpe']:.2f} vs buy and hold "
                 f"{status['benchmark_sharpe']:.2f} over {status['window'][0]}..{status['window'][1]}"
                 if "combined_sharpe" in status else f" ({status.get('reason', '')})"), ""]
    out = paths.desk / "reports" / f"monthly-{month}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    return out
