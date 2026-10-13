from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from quant_research.data.bars import to_epoch
from quant_research.paths import Paths
from quant_research.portfolio.policy import DEFAULT_POLICY, IDENTITY_POLICY, PortfolioPolicy, single_asset_weights
from quant_research.strategy.plugin import plugin_path
from quant_research.strategy.sandbox import SandboxConfig, run_plugin
from quant_research.strategy.spec import StrategyError, StrategySpec, normalize_targets
from quant_research.strategy.templates import get_template


@dataclass
class TargetResult:
    targets: np.ndarray  # raw strategy output: exposure proposals, or final weights for legacy entry points
    lookahead_checked: list[int] = field(default_factory=list)
    lookahead_violations: list[int] = field(default_factory=list)  # bar indices whose value changed
    contract: str = "exposures"  # exposures | legacy_targets

    @property
    def lookahead_ok(self) -> bool:
        return not self.lookahead_violations


def check_positions(window_mask: np.ndarray, n_checks: int = 12) -> list[int]:
    """Evenly spaced bar indices inside the scored window used for prefix-consistency checks."""
    idx = np.flatnonzero(window_mask)
    if not len(idx) or n_checks <= 0:
        return []
    picks = np.unique(np.linspace(0, len(idx) - 1, min(n_checks, len(idx))).round().astype(int))
    return [int(idx[p]) for p in picks]


def compute_targets(
    spec: StrategySpec,
    bars: pd.DataFrame,
    paths: Paths | None = None,
    sandbox: SandboxConfig | None = None,
    positions: list[int] | None = None,
) -> TargetResult:
    """Run the strategy on full history and on truncated prefixes ending at `positions`.

    A strategy without lookahead yields the same value at bar i whether or not later bars exist.
    """
    positions = positions or []
    bars = bars.reset_index(drop=True)
    if spec.kind == "template":
        template = get_template(spec.name)
        targets = normalize_targets(template(bars, spec.parameters), len(bars), strict=True)
        prefix = {i + 1: float(normalize_targets(template(bars.iloc[: i + 1], spec.parameters), i + 1,
                                                 strict=True)[-1])
                  for i in positions}
        contract = "exposures"
    else:
        if paths is None:
            raise ValueError("plugin strategies need Paths to locate frozen code")
        raw, prefix, contract = run_plugin(plugin_path(paths, spec.name), bars, spec.parameters,
                                           sandbox or SandboxConfig(), [i + 1 for i in positions])
        targets = normalize_targets(raw, len(bars))
    violations = [i for i in positions if abs(prefix[i + 1] - targets[i]) > 1e-9]
    return TargetResult(targets, positions, violations, contract)


def effective_policy(policy: PortfolioPolicy, contract: str) -> PortfolioPolicy:
    """Legacy entry points return final weights, so only `identity` applies (contracts-v2 section 3)."""
    if contract == "exposures" or policy.name == "identity":
        return policy
    if policy == DEFAULT_POLICY:
        return IDENTITY_POLICY
    raise StrategyError(f"legacy target entry point returns final weights; {policy.label()} needs a strategy that "
                        "implements generate_exposures(history, parameters)")


def strategy_weights(tr: TargetResult, policy: PortfolioPolicy, bars: pd.DataFrame, step_seconds: int,
                     periods_per_year: float) -> tuple[np.ndarray, PortfolioPolicy]:
    """Target weights for one strategy on one asset, and the policy actually applied."""
    applied = effective_policy(policy, tr.contract)
    weights = single_asset_weights(applied, tr.targets, bars["close"].to_numpy(float),
                                   to_epoch(bars["bar_open_time"]), step_seconds, periods_per_year)
    return weights, applied
