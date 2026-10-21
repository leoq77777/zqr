from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, NonNegativeFloat, PositiveFloat, PositiveInt

BEIJING = timezone(timedelta(hours=8))


def deepseek_peak(ts: float) -> bool:
    """Mon-Fri 09:00-12:00 and 14:00-18:00 Beijing time. Chinese public holidays are not modelled (charged as
    peak, i.e. conservatively)."""
    t = datetime.fromtimestamp(ts, BEIJING)
    return t.weekday() < 5 and (9 <= t.hour < 12 or 14 <= t.hour < 18)


def deepseek_peak_between(start: float, end: float) -> bool:
    """Whether any moment of [start, end] is peak: a run is charged peak if any of its calls may have been."""
    if deepseek_peak(start) or deepseek_peak(end):
        return True
    day = datetime.fromtimestamp(start, BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    while day.timestamp() <= end:
        if day.weekday() < 5 and any(start <= day.replace(hour=h).timestamp() <= end for h in (9, 14)):
            return True
        day += timedelta(days=1)
    return False


class ModelPricing(BaseModel):
    """Vendor price per million tokens for the model actually used; cost() returns USD."""

    model: str
    currency: str = "USD"
    input_per_mtok: NonNegativeFloat  # cache miss
    output_per_mtok: NonNegativeFloat
    cache_read_per_mtok: NonNegativeFloat | None = None  # cache hit; None = same as input
    usd_per_unit: PositiveFloat = 1.0  # FX: USD per 1 unit of `currency`
    schedule: Literal["flat", "deepseek"] = "flat"  # deepseek: off-peak is half the listed (peak) price
    source: str = ""

    def multiplier(self, ts: float | None = None, until: float | None = None) -> float:
        """At `ts`, or over [ts, until] when the calls' times within a run are unknown (peak if any part is)."""
        if self.schedule == "deepseek":
            ts = time.time() if ts is None else ts
            peak = deepseek_peak(ts) if until is None else deepseek_peak_between(ts, until)
            return 1.0 if peak else 0.5
        return 1.0

    def cost(self, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0,
             ts: float | None = None, until: float | None = None) -> float:
        cache = self.input_per_mtok if self.cache_read_per_mtok is None else self.cache_read_per_mtok
        native = (input_tokens * self.input_per_mtok + cache_read_tokens * cache
                  + output_tokens * self.output_per_mtok) / 1e6
        return native * self.multiplier(ts, until) * self.usd_per_unit


DEEPSEEK_PRICES_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
DEFAULT_CNY_PER_USD = 7.1  # assumption; override with --cny-per-usd

# Listed peak prices (CNY per million tokens) as of 2026-10-12.
PRICING_PRESETS: dict[str, dict] = {
    "deepseek-flash": {"input_per_mtok": 2.0, "cache_read_per_mtok": 0.04, "output_per_mtok": 8.0},
    "deepseek-v4-pro": {"input_per_mtok": 9.0, "cache_read_per_mtok": 0.30, "output_per_mtok": 27.0},
}


def pricing_preset(name: str, cny_per_usd: float = DEFAULT_CNY_PER_USD) -> ModelPricing:
    if name not in PRICING_PRESETS:
        raise ValueError(f"unknown pricing preset {name!r}; known: {sorted(PRICING_PRESETS)}")
    return ModelPricing(model=name, currency="CNY", usd_per_unit=1 / cny_per_usd, schedule="deepseek",
                        source=f"{DEEPSEEK_PRICES_URL} (peak prices as of 2026-10-12; FX {cny_per_usd} CNY/USD)",
                        **PRICING_PRESETS[name])


class Budget(BaseModel):
    max_rounds: PositiveInt = 5
    max_candidates: PositiveInt = 20
    max_experiments: PositiveInt = 300
    max_wall_seconds: NonNegativeFloat = 4 * 3600
    max_model_cost_usd: NonNegativeFloat | None = None
    patience_rounds: PositiveInt = 2  # stop after this many rounds without a better validation result
    max_trials_per_strategy: PositiveInt | None = 50  # per template/plugin within a session


class Usage(BaseModel):
    rounds: int = 0
    candidates: int = 0
    experiments: int = 0
    wall_seconds: float = 0.0
    input_tokens: int = 0  # excludes cache hits
    cache_read_tokens: int = 0
    output_tokens: int = 0
    model_cost_usd: float = 0.0
    model_cost_known: bool = True
    rounds_without_improvement: int = 0


def exhausted(budget: Budget, usage: Usage) -> str | None:
    if usage.rounds >= budget.max_rounds:
        return f"round budget reached ({budget.max_rounds})"
    if usage.candidates >= budget.max_candidates:
        return f"candidate budget reached ({budget.max_candidates})"
    if usage.experiments >= budget.max_experiments:
        return f"experiment budget reached ({budget.max_experiments})"
    if usage.wall_seconds >= budget.max_wall_seconds:
        return f"wall-time budget reached ({budget.max_wall_seconds:.0f}s)"
    if budget.max_model_cost_usd is not None:
        if not usage.model_cost_known and usage.rounds > 0:
            return "model cost budget set but the runner did not report token usage"
        if usage.model_cost_usd >= budget.max_model_cost_usd:
            return f"model cost budget reached (${budget.max_model_cost_usd:.2f})"
    return None

