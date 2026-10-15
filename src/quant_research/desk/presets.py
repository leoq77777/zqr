"""The baseline paper roster: simple template strategies on every desk product, so the fleet runs from day one."""

from __future__ import annotations

import json
from importlib import resources

from quant_research.paper.fleet import Roster, RosterEntry
from quant_research.paper.risk import RiskLimits


def preset_roster() -> Roster:
    data = json.loads(resources.files("quant_research.desk").joinpath("presets.json").read_text())
    entries = [
        RosterEntry(name=f"{product.split('-')[0].lower()}-{s['key']}", product=product,
                    granularity=data["granularity"], strategy_ref=s["strategy_ref"], parameters=s["parameters"],
                    initial_cash=data["initial_cash"], source="preset")
        for product in data["products"] for s in data["strategies"]
    ]
    return Roster(entries=entries, risk=RiskLimits(max_order_notional=data["initial_cash"]))
