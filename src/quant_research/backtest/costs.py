"""Named cost profiles (docs/investment-policy.md §6, amended 2026-10-19).

Research uses a fixed conservative baseline per mandate. The accounting engine still fills at the
next bar open with market-style certainty (contracts-v2 §4); profiles are all-in friction constants
with labelled sources, not maker/limit simulators or volume-tier schedules.
"""

from __future__ import annotations

from typing import Literal

from quant_research.backtest.engine import CostConfig

CostProfile = Literal["coinbase-lf-allin-v1", "coinbase-retail-taker-v0"]

AUTHORITATIVE_PROFILE: CostProfile = "coinbase-lf-allin-v1"
RETAIL_TAKER_PROFILE: CostProfile = "coinbase-retail-taker-v0"

_SOURCE_LF_ALLIN = (
    "cost_profile:coinbase-lf-allin-v1: assumed all-in low-frequency rebalancing friction proxy for "
    "Coinbase Advanced Trade (BTC-USD/ETH-USD spot). fee_rate=0.0020 and slippage_bps=2.0 are a "
    "conservative pack of steady-state maker-biased economics after normal personal 30-day volume "
    "clears the lowest published tier, occasional taker cleanup, tight spot spreads, and a ~20-30% "
    "buffer. The backtest engine still uses next-bar-open market-style fills with certain execution "
    "(contracts-v2 §4); this is NOT a maker queue or partial-fill simulator. Verified schedule "
    "context: Coinbase Advanced Trade public tiers as of 2026-10-15 (taker 0.60%/maker 0.40% below "
    "$10k 30d; maker falls with volume). Policy: docs/investment-policy.md §6 (amended 2026-10-19)."
)

_SOURCE_RETAIL_TAKER = (
    "cost_profile:coinbase-retail-taker-v0: worst-case retail taker reference. fee_rate=0.0060 "
    "(Coinbase Advanced Trade lowest public taker tier) plus slippage_bps=5.0. Matches the "
    "2026-10-13/2026-10-15 sole baseline; retained for stress/contrast only. Engine: next-bar-open "
    "market-style fills (contracts-v2 §4). Not the deployable admission world unless this profile is "
    "the mandate base. Policy: docs/investment-policy.md §6."
)

COST_PROFILES: dict[CostProfile, CostConfig] = {
    AUTHORITATIVE_PROFILE: CostConfig(fee_rate=0.002, slippage_bps=2.0, source=_SOURCE_LF_ALLIN),
    RETAIL_TAKER_PROFILE: CostConfig(fee_rate=0.006, slippage_bps=5.0, source=_SOURCE_RETAIL_TAKER),
}


def profile_prefix(profile: CostProfile) -> str:
    return f"cost_profile:{profile}:"


def cost_config(profile: CostProfile) -> CostConfig:
    return COST_PROFILES[profile].model_copy()


def costs_match_profile(costs: CostConfig, profile: CostProfile) -> bool:
    want = COST_PROFILES[profile]
    return (costs.fee_rate == want.fee_rate and costs.slippage_bps == want.slippage_bps
            and costs.source.startswith(profile_prefix(profile)))
