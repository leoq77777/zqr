"""Portfolio policy v1 (docs/contracts-v2.md section 5).

Every step uses completed bars up to the decision bar only, so the weights at bar t do not change when later bars
are appended. `identity` passes legacy final targets through unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, PositiveFloat, PositiveInt, model_validator

from quant_research.backtest.engine import EngineConfig
from quant_research.hashing import hash_obj


class PortfolioPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["policy", "identity"] = "policy"
    version: str = "1"
    vol_target: PositiveFloat | None = None  # annualised; None = no volatility scaling
    vol_lookback: int = Field(30, ge=2)
    asset_cap: float = Field(1.0, gt=0, le=1)
    gross_cap: float = Field(1.0, gt=0, le=1)
    rebalance_every: PositiveInt = 1
    band: NonNegativeFloat = 0.0  # skip rebalances smaller than this many weight units

    @model_validator(mode="after")
    def _identity_has_no_parameters(self):
        if self.name == "identity" and any(getattr(self, k) != f.default for k, f in type(self).model_fields.items()
                                           if k not in ("name", "version")):
            raise ValueError("the identity policy takes no parameters")
        return self

    def policy_id(self) -> str:
        return "policy-" + hash_obj(self.model_dump())[:12]

    def label(self) -> str:
        if self.name == "identity":
            return f"identity-v{self.version}"
        parts = [f"vol_target={self.vol_target}" if self.vol_target else "no vol target",
                 f"asset_cap={self.asset_cap:g}", f"gross_cap={self.gross_cap:g}"]
        if self.rebalance_every > 1:
            parts.append(f"rebalance every {self.rebalance_every} bars")
        if self.band:
            parts.append(f"band={self.band:g}")
        return f"policy-v{self.version} ({', '.join(parts)})"

    def engine_config(self, cfg: EngineConfig) -> EngineConfig:
        """The no-trade band is enforced by the engine against the weight at the fill bar's open."""
        if self.band <= cfg.rebalance_tolerance:
            return cfg
        return cfg.model_copy(update={"rebalance_tolerance": self.band})


DEFAULT_POLICY = PortfolioPolicy()
IDENTITY_POLICY = PortfolioPolicy(name="identity")


def realized_vol(close: np.ndarray, lookback: int, periods_per_year: float) -> np.ndarray:
    """Annualised std (ddof=1) of the last `lookback` log returns ending at each bar, NaN during warmup."""
    logr = np.diff(np.log(np.asarray(close, float)), prepend=np.nan)
    sd = pd.Series(logr).rolling(lookback, min_periods=lookback).std().to_numpy()
    return sd * math.sqrt(periods_per_year)


def vol_scale(policy: PortfolioPolicy, sigma: np.ndarray) -> np.ndarray:
    if policy.vol_target is None:
        return np.ones(len(sigma))
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = np.minimum(1.0, policy.vol_target / sigma)
    return np.where(np.isfinite(scale) & (sigma > 0), scale, 0.0)


def net_proposals(proposals: Sequence[tuple[float, np.ndarray]]) -> np.ndarray:
    """Sum of allocation-weighted proposals for one asset; netting happens before any order."""
    allocs = [a for a, _ in proposals]
    if not math.isclose(sum(allocs), 1.0, abs_tol=1e-9) or min(allocs) < 0:
        raise ValueError("strategy allocations must be non-negative and sum to 1")
    return np.sum([a * np.asarray(e, float) for a, e in proposals], axis=0)


def cap_gross(weights: Mapping[str, np.ndarray], gross_cap: float) -> dict[str, np.ndarray]:
    """Scale all assets down proportionally on bars where the summed weight exceeds `gross_cap`."""
    assets = list(weights)
    w = np.vstack([np.asarray(weights[a], float) for a in assets])
    total = w.sum(axis=0)
    factor = np.where(total > gross_cap, gross_cap / np.where(total > 0, total, 1.0), 1.0)
    return {a: w[k] * factor for k, a in enumerate(assets)}


def apply_schedule(weights: np.ndarray, open_times: np.ndarray, step_seconds: int, every: int) -> np.ndarray:
    """NaN (= no order, the position drifts) between scheduled bars; the schedule is anchored to the epoch."""
    if every <= 1:
        return weights
    scheduled = (np.asarray(open_times, np.int64) // step_seconds) % every == 0
    return np.where(scheduled, weights, np.nan)


def asset_weights(policy: PortfolioPolicy, raw: np.ndarray, close: np.ndarray, open_times: np.ndarray,
                  step_seconds: int, periods_per_year: float) -> np.ndarray:
    """Target weight of one asset before the cross-asset gross cap."""
    raw = np.asarray(raw, float)
    if policy.name == "identity":
        return raw.copy()
    w = raw * vol_scale(policy, realized_vol(close, policy.vol_lookback, periods_per_year))
    w = np.minimum(policy.asset_cap, w)
    return apply_schedule(w, open_times, step_seconds, policy.rebalance_every)


def single_asset_weights(policy: PortfolioPolicy, exposures: np.ndarray, close: np.ndarray, open_times: np.ndarray,
                         step_seconds: int, periods_per_year: float) -> np.ndarray:
    w = asset_weights(policy, exposures, close, open_times, step_seconds, periods_per_year)
    return w if policy.name == "identity" else np.minimum(w, policy.gross_cap)
