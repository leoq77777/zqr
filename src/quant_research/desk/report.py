"""Daily desk report: `desk/reports/YYYY-MM-DD.md` (UTC day).

Paper: per strategy equity, return vs buy-and-hold over the same bars, trades, fees, alerts, and a decision-parity
check (the bars the fleet recorded are re-run through the same strategy code; the target must be identical).
Research: sessions, cost, trials, candidates by state, holdout viewings left, violations, audit failures.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from quant_research.desk import research as rl
from quant_research.desk.config import load_config
from quant_research.desk.promotion import roster_path
from quant_research.desk.supervisor import state_file
from quant_research.experiments.store import ExperimentStore
from quant_research.paper.fleet import RosterEntry, load_roster
from quant_research.paper.ledger import Ledger
from quant_research.paper.trader import target_fraction
from quant_research.paths import Paths
from quant_research.research.mandate import Mandate
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.strategy.spec import StrategySpec

MAX_PARITY_CHECKS = 48


def _window(day: str) -> tuple[float, float]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:+.2%}"


def parity(entry: RosterEntry, ledger: Ledger, decisions: list[dict[str, Any]], paths: Paths) -> dict[str, Any]:
    """Recompute each decision's target from the recorded bars; any difference is a bug, not noise."""
    checkable = [d for d in decisions if d.get("history_start")][-MAX_PARITY_CHECKS:]
    if not checkable:
        return {"checked": 0, "mismatches": [], "note": "no decisions with a recorded window"}
    bars = ledger.bars()
    spec = StrategySpec(strategy_ref=entry.strategy_ref, parameters=entry.parameters)
    sandbox = SandboxConfig(isolation="docker" if entry.strategy_ref.startswith("plugin:") else "subprocess")
    mismatches = []
    for d in checkable:
        lo, hi = pd.Timestamp(d["history_start"]), pd.Timestamp(d["bar_open"])
        window = bars[(bars["bar_open_time"] >= lo) & (bars["bar_open_time"] <= hi)].reset_index(drop=True)
        try:
            frac = target_fraction(spec, entry.policy, window, entry.granularity, paths, sandbox)
        except Exception as exc:  # noqa: BLE001
            mismatches.append(f"{d['bar_open']}: recompute failed ({type(exc).__name__}: {exc})")
            continue
        recorded = d["target"]["target_fraction"]
        if frac != recorded:
            mismatches.append(f"{d['bar_open']}: recorded {recorded}, recomputed {frac}")
    return {"checked": len(checkable), "mismatches": mismatches}


def paper_section(paths: Paths, day: str) -> tuple[list[str], dict[str, Any]]:
    t0, t1 = _window(day)
    rp = roster_path(paths)
    entries = {e.name: e for e in load_roster(rp).entries} if rp.exists() else {}
    rows, details, summary = [], [], {"strategies": 0, "parity_mismatches": 0, "alerts": 0}
    for name, entry in sorted(entries.items()):
        db = paths.paper / name / "paper.db"
        if not db.exists():
            continue
        ledger = Ledger(db)
        try:
            all_dec = ledger.decisions()
            day_dec = [d for d in all_dec if t0 <= d["at"] < t1]
            before = [d for d in all_dec if d["at"] < t0 and d.get("equity")]
            priced = [d for d in all_dec if d.get("equity") and d["at"] < t1]
            start = before[-1] if before else next((d for d in day_dec if d.get("equity")), None)
            end = next((d for d in reversed(day_dec) if d.get("equity")), None)
            fills = [f for f in ledger.fills() if t0 <= f.ts < t1]
            par = parity(entry, ledger, day_dec, paths)
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
        if start and end:
            day_ret = float(end["equity"]) / float(start["equity"]) - 1
            bh_ret = float(end["price"]) / float(start["price"]) - 1
        if end:
            total_ret = float(end["equity"]) / entry.initial_cash - 1
            if priced:
                bh_total = float(end["price"]) / float(priced[0]["price"]) - 1
        fees = sum(float(f.fee) for f in fills)
        summary["strategies"] += 1
        summary["parity_mismatches"] += len(par["mismatches"])
        summary["alerts"] += sum(alerts.values())
        rows.append(f"| {name} | {entry.strategy_ref.split(':')[-1][:16]} | {entry.product} | "
                    f"{float(end['equity']):,.2f} | {_pct(day_ret)} | {_pct(bh_ret)} | {_pct(total_ret)} | "
                    f"{_pct(bh_total)} | {len(fills)} | {fees:.2f} | {sum(alerts.values())} | "
                    f"{par['checked'] - len(par['mismatches'])}/{par['checked']} |"
                    if end else
                    f"| {name} | {entry.strategy_ref.split(':')[-1][:16]} | {entry.product} | - | - | - | - | - | "
                    f"{len(fills)} | {fees:.2f} | {sum(alerts.values())} | {par['checked']} |")
        if alerts or par["mismatches"] or not entry.enabled:
            details.append(f"- **{name}**{' (disabled)' if not entry.enabled else ''}: "
                           + ", ".join(f"{k}×{v}" for k, v in alerts.items())
                           + "".join(f"\n  - parity: {m}" for m in par["mismatches"][:5]))
    lines = ["## Paper fleet", "",
             "Simulated fills at the mid price plus slippage and fees on live Coinbase public data. Day return and "
             "buy & hold use the same decision bars; totals run from each strategy's first decision.", "",
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


def research_section(paths: Paths, store: ExperimentStore, day: str) -> tuple[list[str], dict[str, Any]]:
    cfg = load_config(paths)
    rows = [r for r in rl.read_ledger(paths) if r.get("day") == day]
    cost = sum(r.get("cost_usd") or 0.0 for r in rows)
    failures = Counter(r["failure_class"] for r in rows)
    state = rl.load_state(paths)
    lines = ["## Research", "",
             f"- Sessions: {len(rows)} ({', '.join(f'{k} {v}' for k, v in failures.items()) or 'none'}); "
             f"estimated model cost {cost:.2f} USD (daily cap {cfg.daily_cost_cap_usd} USD)",
             f"- Loop status now: {state.get('status', '-')}" + (f" ({state['reason']})" if state.get("reason")
                                                                  else ""),
             f"- Violations: {sum(r.get('violations') or 0 for r in rows)}; audit failures: "
             f"{sum(not r.get('audit_ok', True) for r in rows)}"]
    summary = {"sessions": len(rows), "cost_usd": round(cost, 4), "failures": dict(failures)}
    mandate_ids = set(cfg.mandates.values())
    if mandate_ids:
        lines += ["", "| product | trials on split (all time) | candidates by state | holdout viewings left |",
                  "|---|---|---|---|"]
        for product, mid in cfg.mandates.items():
            rec = store.get_mandate(mid)
            if rec is None:
                continue
            m = Mandate.model_validate(rec["body"])
            states = Counter(c["state"] for c in store.list_candidates() if c["mandate_id"] == mid)
            left = m.holdout.max_viewings - len(store.holdout_runs(m.split_id))
            lines.append(f"| {product} | {store.trial_count(m.split_id)} | "
                         f"{', '.join(f'{k} {v}' for k, v in sorted(states.items())) or '-'} | "
                         f"{left}{' **(exhausted: no more automatic promotion)**' if left <= 0 else ''}"
                         f"{' **(contaminated by override)**' if store.holdout_overrides(m.split_id) else ''} |")
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
    research, rs = research_section(paths, store, day)
    lines = [f"# Desk report {day} (UTC)", "",
             f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC. Paper trading only; no live orders exist. "
             "Backtests and paper results are not evidence of live profitability.", "",
             f"Summary: {ps['strategies']} paper strategies, {ps['alerts']} alerts, "
             f"{ps['parity_mismatches']} parity mismatches; {rs['sessions']} research sessions, "
             f"{rs['cost_usd']} USD.", "", *paper, "", *research, ""]
    out = paths.desk / "reports" / f"{day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    return out
