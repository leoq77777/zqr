"""Daily desk report: `desk/reports/YYYY-MM-DD.md` (UTC day).

Paper: per strategy equity, return vs buy-and-hold over the same bars, trades, fees, alerts, and decision parity
(targets recomputed from the bar store; order side and size re-derived with the backtest rule, with the order cap,
cash buffer and risk limits counted as allowed differences; fills compared with the next bar's open; a backtest run
from the paper state at the first decision, with the equity gap split into execution and sizing/timing).
Research: sessions, cost, trials, candidates by state, holdout viewings left, violations, audit failures.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from quant_research.desk import research as rl
from quant_research.desk.config import load_config
from quant_research.desk.portfolio_report import portfolio_section, versions_lines
from quant_research.desk.promotion import roster_path
from quant_research.desk.supervisor import state_file
from quant_research.evaluation.split_stats import NEXT_SPLIT_EARLIEST
from quant_research.experiments.store import ExperimentStore
from quant_research.memory.clusters import k_label
from quant_research.paper.barstore import BarStore
from quant_research.paper.fleet import Roster, entry_costs, load_roster
from quant_research.paper.ledger import Ledger
from quant_research.paper.parity import paper_parity
from quant_research.paths import Paths
from quant_research.research.mandate import Mandate
from quant_research.validation.holdout import closed_refusal

ALLOWED_LABELS = {"order_cap": "order cap", "cash_buffer": "cash buffer", "risk_adjusted": "risk limits"}


def _window(day: str) -> tuple[float, float]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:+.2%}"


def paper_section(paths: Paths, day: str) -> tuple[list[str], dict[str, Any]]:
    t0, t1 = _window(day)
    rp = roster_path(paths)
    roster = load_roster(rp) if rp.exists() else Roster()
    rows, details, summary = [], [], {"strategies": 0, "parity_mismatches": 0, "alerts": 0}
    stores: dict[tuple[str, str], Any] = {}
    for entry in sorted(roster.entries, key=lambda e: e.name):
        name = entry.name
        db = paths.paper / name / "paper.db"
        if entry.source == "portfolio" or not db.exists():
            continue
        key = (entry.product, entry.granularity)
        if key not in stores:
            stores[key] = BarStore.for_feed(paths, *key).bars
        ledger = Ledger(db)
        try:
            marks = ledger.marks()
            all_dec = ledger.decisions()
            fills = [f for f in ledger.fills() if t0 <= f.ts < t1]
            try:
                par = paper_parity(entry, roster, ledger, stores[key], paths, since=t0, until=t1)
            except Exception as exc:  # noqa: BLE001 - one strategy's parity must not sink the report
                par = {"checked": 0, "skipped": 0, "fills": 0, "order_mismatches": [], "size_mismatches": [],
                       "allowed": {}, "path": None,
                       "target_mismatches": [f"parity failed ({type(exc).__name__}: {exc})"]}
        finally:
            ledger.close()
        alerts = Counter()
        ev_path = paths.paper / name / "events.jsonl"
        if ev_path.exists():
            for line in ev_path.read_text().splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if t0 <= ev.get("ts", 0) < t1 and ev.get("level") in ("WARN", "ALERT"):
                    alerts[ev["event"]] += 1
        day_ret = bh_ret = total_ret = bh_total = None
        upto = marks[marks["at"] <= t1]
        end = upto.iloc[-1] if len(upto) and upto.iloc[-1]["at"] > t0 else None
        if end is not None:
            before = marks[marks["at"] <= t0]
            start = before.iloc[-1] if len(before) else marks[marks["at"] > t0].iloc[0]
            fee, slip, _ = entry_costs(entry, roster)
            day_ret = end["equity"] / start["equity"] - 1
            bh_ret = end["price"] / start["price"] - 1
            total_ret = end["equity"] / entry.initial_cash - 1
            p0 = float(all_dec[0]["price"]) if all_dec else marks.iloc[0]["price"]
            bh_total = end["price"] / (p0 * (1 + slip / 10_000) * (1 + fee)) - 1
        fees = sum(float(f.fee) for f in fills)
        mismatches = par["target_mismatches"] + par["order_mismatches"] + par["size_mismatches"]
        summary["strategies"] += 1
        summary["parity_mismatches"] += len(mismatches)
        summary["alerts"] += sum(alerts.values())
        rows.append(f"| {name} | {entry.strategy_ref.split(':')[-1][:16]} | {entry.product} | "
                    f"{end['equity']:,.2f} | {_pct(day_ret)} | {_pct(bh_ret)} | {_pct(total_ret)} | "
                    f"{_pct(bh_total)} | {len(fills)} | {fees:.2f} | {sum(alerts.values())} | "
                    f"{par['checked'] - len(mismatches)}/{par['checked']} |"
                    if end is not None else
                    f"| {name} | {entry.strategy_ref.split(':')[-1][:16]} | {entry.product} | - | - | - | - | - | "
                    f"{len(fills)} | {fees:.2f} | {sum(alerts.values())} | {par['checked']} |")
        if alerts or mismatches or not entry.enabled:
            details.append(f"- **{name}**{' (disabled)' if not entry.enabled else ''}: "
                           + ", ".join(f"{k}×{v}" for k, v in alerts.items())
                           + "".join(f"\n  - parity: {m}" for m in mismatches[:5]))
        if par["fills"]:
            details.append(f"- {name} execution: {par['fills']} fills, {par['fill_vs_next_open_bps']:+.1f} bps vs "
                           f"the next bar's open, fee rate error {par['fee_rate_error']:.2e}"
                           + (f"; {par['skipped']} decisions predate the bar store" if par["skipped"] else ""))
        path = par.get("path")
        if path and (par["fills"] or par["allowed"] or abs(path["equity_gap_bps"]) >= 0.5):
            allowed = ", ".join(f"{ALLOWED_LABELS.get(k, k)} ×{v}" for k, v in sorted(par["allowed"].items()))
            details.append(f"- {name} vs backtest over {path['bars']} bars: equity {path['equity_gap_bps']:+.1f} bps "
                           f"(execution {path['execution_bps']:+.1f}, sizing and timing "
                           f"{path['sizing_timing_bps']:+.1f}), exposure {path['exposure_gap_pp']:+.2f} pp"
                           + (f"; allowed differences: {allowed}" if allowed else ""))
    lines = ["## Paper fleet", "",
             "Simulated fills at the mid price plus slippage and fees on live Coinbase public data. Returns use "
             "the equity marked at every bar close; total buy & hold includes the entry fee and slippage.", "",
             "| strategy | rule | product | equity | day | day B&H | total | total B&H | fills | fees | alerts | "
             "parity |", "|---|---|---|---|---|---|---|---|---|---|---|---|", *rows]
    if not rows:
        lines.append("| (no paper strategies with a ledger) |||||||||||| ")
    if details:
        lines += ["", "Attention:", *details]
    sup = json.loads(state_file(paths).read_text()) if state_file(paths).exists() else {}
    comps = sup.get("components", {})
    if comps:
        lines += ["", "Process restarts (since the supervisor started): "
                  + ", ".join(f"{n} {c.get('restarts', 0)}" for n, c in comps.items())]
    fleet_log = paths.paper / "fleet.jsonl"
    if fleet_log.exists():
        fleet_alerts = Counter()
        for line in fleet_log.read_text().splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if t0 <= ev.get("ts", 0) < t1 and ev.get("level") in ("WARN", "ALERT"):
                fleet_alerts[ev["event"]] += 1
        if fleet_alerts:
            lines.append("Fleet alerts: " + ", ".join(f"{k}×{v}" for k, v in fleet_alerts.items()))
    return lines, summary


def hypothesis_lines(store: ExperimentStore, next_split: str) -> list[str]:
    from quant_research.hypothesis.gates import report_lines

    return report_lines(store, next_split)


def research_section(paths: Paths, store: ExperimentStore, day: str) -> tuple[list[str], dict[str, Any]]:
    from quant_research.campaign.budget import month_of
    from quant_research.campaign.report import budget_lines, campaign_lines, session_rows
    from quant_research.inbox.report import inbox_lines

    cfg = load_config(paths)
    every = [r for r in rl.read_ledger(paths) if r.get("day") == day]
    rows = [r for r in every if r.get("topic") != rl.INBOX_TOPIC]
    drafts = [r for r in every if r.get("topic") == rl.INBOX_TOPIC]
    cost = sum(r.get("cost_usd") or 0.0 for r in every)
    failures = Counter(r["failure_class"] for r in rows)
    state = rl.load_state(paths)
    lines = ["## Research", "",
             f"- Sessions: {len(rows)} ({', '.join(f'{k} {v}' for k, v in failures.items()) or 'none'}); "
             f"estimated model cost {cost:.2f} USD",
             *budget_lines(paths, cfg, day, cost, rl.spent_in_month(paths, month_of(day))),
             f"- Loop status now: {state.get('status', '-')}" + (f" ({state['reason']})" if state.get("reason")
                                                                  else ""),
             f"- Violations: {sum(r.get('violations') or 0 for r in rows)}; audit failures: "
             f"{sum(not r.get('audit_ok', True) for r in rows)}",
             *campaign_lines(paths, store, cfg, day),
             *inbox_lines(store, day, drafts)]
    summary = {"sessions": len(rows), "inbox_drafts": len(drafts), "cost_usd": round(cost, 4),
               "failures": dict(failures)}
    mandate_ids = set(cfg.mandates.values())
    if mandate_ids:
        lines += ["", "| product | cost profile | trials on split (all time) | effective K (ρ 0.7) | "
                  "candidates by state | holdout viewings left |", "|---|---|---|---|---|---|"]
        for product, mid in cfg.mandates.items():
            rec = store.get_mandate(mid)
            if rec is None:
                continue
            m = Mandate.model_validate(rec["body"])
            states = Counter(c["state"] for c in store.list_candidates() if c["mandate_id"] == mid)
            left = m.holdout.max_viewings - len(store.holdout_runs(m.split_id))
            trials = store.split_trial_count(m.split_id)
            parent = store.split_parent(m.split_id)
            inherited = (f"; parent `{parent['parent_id']}` viewed {len(store.holdout_runs(parent['parent_id']))}"
                         if parent else "")
            split_trials = (f"{trials['total']} ({trials['own']} + {trials['inherited']} inherited)" if parent
                            else str(trials["total"]))
            profile = m.cost_profile or "(legacy)"
            lines.append(f"| {product} | `{profile}` | {split_trials} | {k_label(store, m.split_id)} | "
                         f"{', '.join(f'{k} {v}' for k, v in sorted(states.items())) or '-'} | "
                         f"{left}{inherited}{' **(exhausted: no more automatic promotion)**' if left <= 0 else ''}"
                         f"{' **(closed)**' if closed_refusal(store, m.split_id) else ''}"
                         f"{' **(contaminated by override)**' if store.holdout_overrides(m.split_id) else ''} |")
        lines += versions_lines(store, cfg.mandates)
        lines += hypothesis_lines(store, NEXT_SPLIT_EARLIEST)
        lines += ["", f"- Next split: earliest {NEXT_SPLIT_EARLIEST} (182 days validation + 182 days holdout "
                      "after the current holdout; appendix A11)"]
    lines += session_rows(rows)
    produced = [r for r in rows if r.get("candidate_id") or r.get("verdict")]
    if produced:
        lines += ["", "| run | product | topic | verdict | candidate | promotion | cost |",
                  "|---|---|---|---|---|---|---|"]
        lines += [f"| {r['run_id']} | {r['product']} | {r['topic']} | {r.get('verdict') or '-'} | "
                  f"{r.get('candidate_id') or '-'} | {r.get('promotion') or '-'} | {r.get('cost_usd') or 0:.3f} |"
                  for r in produced]
    bad = [r for r in rows if r["failure_class"] != "none"]
    if bad:
        lines += ["", "Failed sessions:"]
        for r in bad[-10:]:
            res_path = rl.sessions_dir(paths) / r["run_id"] / "result.json"
            det = json.loads(res_path.read_text()).get("failure_details", []) if res_path.exists() else []
            lines.append(f"- {r['run_id']}: {r['failure_class']} — {'; '.join(det)[:200]}")
    return lines, summary


def write_daily_report(paths: Paths, store: ExperimentStore, day: str) -> Path:
    paper, ps = paper_section(paths, day)
    portfolios, pfs = portfolio_section(paths, store, day)
    research, rs = research_section(paths, store, day)
    lines = [f"# Desk report {day} (UTC)", "",
             f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC. Paper trading only; no live orders exist. "
             "Backtests and paper results are not evidence of live profitability.", "",
             f"Summary: {pfs['portfolios']} paper portfolios, {pfs['shadows']} shadow portfolios, "
             f"{ps['strategies']} paper strategies, "
             f"{ps['alerts'] + pfs['alerts']} alerts, {ps['parity_mismatches'] + pfs['parity_mismatches']} parity "
             f"mismatches; {rs['sessions']} research sessions, {rs['cost_usd']} USD.", "",
             *([*portfolios, ""] if portfolios else []), *paper, "", *research, ""]
    out = paths.desk / "reports" / f"{day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    return out
