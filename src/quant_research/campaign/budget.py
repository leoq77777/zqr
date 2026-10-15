"""Research budget (D7, P2): reservations against the campaign, the UTC day and the month; prices; the budget stop.

A session starts only if `session_reserve_usd` fits under all three caps; the booked cost replaces the reservation
when it ends. A session that costs more than its reservation, or a model call without a configured price, writes
`desk/budget_stop.json` and the research loop stops until the human clears it with a note. The day and month caps
of D7 are ceilings: the desk config may lower them, not raise them.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quant_research.bench.runner import _pricing
from quant_research.controller.budget import ModelPricing, pricing_preset
from quant_research.desk.config import DeskConfig
from quant_research.paths import Paths

CEILINGS = {"daily_usd": 5.0, "monthly_usd": 83.33, "sessions_per_day": 12}


def limits(cfg: DeskConfig) -> dict[str, float]:
    return {"daily_usd": min(cfg.daily_cost_cap_usd, CEILINGS["daily_usd"]),
            "monthly_usd": min(cfg.monthly_cost_cap_usd, CEILINGS["monthly_usd"]),
            "sessions_per_day": min(cfg.max_sessions_per_day, CEILINGS["sessions_per_day"]),
            "session_reserve_usd": cfg.session_reserve_usd}


def default_model(cfg: DeskConfig) -> str | None:
    return cfg.model or os.environ.get("ANTHROPIC_MODEL") or None


def price_for(cfg: DeskConfig, models: list[str]) -> ModelPricing | None:
    """A model named in `campaign_models` takes its preset; otherwise the name decides (deepseek flash or pro)."""
    for m in models:
        if m in cfg.campaign_models:
            return pricing_preset(cfg.campaign_models[m], cfg.cny_per_usd)
    return _pricing(models, cfg.cny_per_usd) if models else None


def month_of(day: str) -> str:
    return day[:7]


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


# -- the stop ------------------------------------------------------------------------------------------------

def stop_path(paths: Paths) -> Path:
    return paths.desk / "budget_stop.json"


def active_stop(paths: Paths) -> dict[str, Any] | None:
    p = stop_path(paths)
    return json.loads(p.read_text()) if p.exists() else None


def stop(paths: Paths, reason: str, **data: Any) -> dict[str, Any]:
    record = {"reason": reason, **data, "at": time.time()}
    if active_stop(paths) is None:
        stop_path(paths).parent.mkdir(parents=True, exist_ok=True)
        stop_path(paths).write_text(json.dumps(record, indent=2, default=str))
    return record


def clear_stop(paths: Paths, note: str) -> dict[str, Any]:
    if not note.strip():
        raise ValueError("clearing the budget stop needs a note")
    current = active_stop(paths)
    if current is None:
        return {"status": "none"}
    kept = stop_path(paths).with_name(f"budget_stop.cleared-{int(time.time())}.json")
    kept.write_text(json.dumps(current | {"cleared_note": note, "cleared_at": time.time()}, indent=2, default=str))
    stop_path(paths).unlink()
    return {"status": "cleared", "kept": kept.name, **current}
