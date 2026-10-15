"""The daily report's inbox lines: items received, drafting outcomes and cost, and the proposals written."""

from __future__ import annotations

from collections import Counter
from typing import Any

from quant_research.campaign.store import CampaignStore
from quant_research.experiments.store import ExperimentStore
from quant_research.inbox.store import InboxStore


def inbox_lines(store: ExperimentStore, day: str, drafts: list[dict[str, Any]]) -> list[str]:
    inbox = InboxStore(store)
    received = sum(e["event"] == "received" and e["actor"] == "human" for e in inbox.events_on(day))
    waiting = len(inbox.list("received"))
    if not (received or drafts or waiting):
        return []
    outcomes = Counter(r.get("inbox_state") or "-" for r in drafts)
    cost = sum(r.get("cost_usd") or 0.0 for r in drafts)
    out = [f"- Inbox: {received} received, {len(drafts)} drafting runs "
           f"({', '.join(f'{k} {v}' for k, v in sorted(outcomes.items())) or 'none'}), {cost:.3f} USD; "
           f"{waiting} waiting"]
    cs = CampaignStore(store)
    for r in drafts:
        for cid in r.get("campaigns_written") or []:
            row = cs.get(cid)
            if row is None:
                continue
            seen = row["source"].get("may_have_seen_validation")
            out.append(f"  - {cid} from {r['inbox_id']}: {row['product']} `{row['family']}` {row['state']}; "
                       f"published {row['source'].get('published') or 'unknown'}"
                       + (" (**its author may have seen our validation or holdout period**)" if seen else "")
                       + f": {row['body']['goal'][:200]}")
    return out
