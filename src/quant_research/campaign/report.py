"""The daily report's campaign lines (D7): campaigns by state, usage against caps, registrations and verdicts,
quotas, budget against the day and month caps, and cost per conclusion."""

from __future__ import annotations

from collections import Counter
from typing import Any

from quant_research.campaign import budget
from quant_research.campaign.conclude import campaign_hypothesis
from quant_research.campaign.model import TERMINAL
from quant_research.campaign.store import CampaignStore
from quant_research.desk.config import DeskConfig
from quant_research.experiments.store import ExperimentStore
from quant_research.hypothesis.gates import verdict
from quant_research.paths import Paths


def budget_lines(paths: Paths, cfg: DeskConfig, day: str, spent_day: float, spent_month: float) -> list[str]:
    lim = budget.limits(cfg)
    stop = budget.active_stop(paths)
    return [f"- Budget: {spent_day:.2f} of {lim['daily_usd']} USD today, {spent_month:.2f} of {lim['monthly_usd']} "
            f"USD this month; reservation {lim['session_reserve_usd']} USD per session, at most "
            f"{lim['sessions_per_day']:.0f} sessions a day",
            *([f"- **Budget stop**: {stop['reason']} (human: `zqr campaign budget clear --note`)"] if stop else [])]


def campaign_lines(paths: Paths, store: ExperimentStore, cfg: DeskConfig, day: str) -> list[str]:
    cs = CampaignStore(store)
    rows = cs.list()
    if not rows:
        return ["- Campaigns: none (`zqr campaign generate --product ...`, then `zqr campaign approve`)"]
    by_state = Counter(r["state"] for r in rows)
    ended = [r for r in rows if r["state"] in ("concluded", "exhausted")]
    ended_cost = sum(cs.usage(r["id"])["usd"] for r in ended)
    out = [f"- Campaigns: {', '.join(f'{s} {n}' for s, n in sorted(by_state.items()))}; cost per ended campaign "
           + (f"{ended_cost / len(ended):.3f} USD over {len(ended)}" if ended else "-")]
    shown = [r for r in rows if r["state"] not in TERMINAL or r["updated_at"] and
             budget.utc_day(r["updated_at"]) == day]
    if shown:
        out += ["", "| campaign | product | family | state | sessions | trials | USD | hypothesis | source |",
                "|---|---|---|---|---|---|---|---|---|"]
        for r in shown:
            caps, used = cs.caps(r["id"]), cs.usage(r["id"])
            hid = campaign_hypothesis(store, r["id"])
            hyp = f"{hid} {verdict(store, hid)['state']}" if hid else "-"
            src = r["source"].get("diagnostic") or r["source"].get("kind")
            out.append(f"| {r['id']} | {r['product']} | {r['family']} | {r['state']} | {used['sessions']}/"
                       f"{caps['sessions']:.0f} | {used['experiments']}/{caps['experiments']:.0f} | "
                       f"{used['usd']:.2f}/{caps['usd']:.0f} | {hyp} | {src} |")
    splits = sorted({r["split_id"] for r in rows if r["product"] in cfg.mandates})
    if shown and splits:
        out.append("")
    for split_id in splits:
        qs = cs.quotas(paths, split_id)
        out.append(f"- Quotas on `{split_id}`: " + ", ".join(f"{q['family']} {q['used']}/{q['quota']:.0f}"
                                                                for q in qs))
    return out


def session_rows(rows: list[dict[str, Any]]) -> list[str]:
    camp = [r for r in rows if r.get("campaign_id")]
    if not camp:
        return []
    return ["", "| run | product | campaign | family | experiments | gates | campaign end | cost |",
            "|---|---|---|---|---|---|---|---|",
            *[f"| {r['run_id']} | {r['product']} | {r['campaign_id']} | {r.get('family') or '-'} | "
              f"{r.get('experiments') or 0} | {r.get('gates') or '-'} | {r.get('campaign_end') or '-'} | "
              f"{r.get('cost_usd') or 0:.3f} |" for r in camp]]
