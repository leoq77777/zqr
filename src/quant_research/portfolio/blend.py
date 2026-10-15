"""Blend member targets into one target per product (design 5.2).

Equal-weight members within a family, equal capital sleeves across families, each member capped at the version's
`member_cap` (the excess stays in cash), probation members at half their share, then the volatility overlay.
Every member uses bars up to the decision bar only, so the blend at bar t does not change when bars are appended.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant_research.portfolio.version import PortfolioVersion
from quant_research.strategy.compute import compute_targets
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.strategy.spec import StrategySpec


class BlendError(RuntimeError):
    pass


@dataclass
class Blend:
    members: dict[str, np.ndarray]  # each member's own target in [0, 1]
    families: dict[str, np.ndarray]  # share-weighted sum of the family's members
    pre_overlay: np.ndarray
    overlay: np.ndarray
    target: np.ndarray


def member_shares(version: PortfolioVersion) -> dict[str, float]:
    sizes: dict[str, int] = defaultdict(int)
    for m in version.members:
        sizes[m.family] += 1
    sleeve = 1.0 / len(sizes)
    return {m.entry_id: min(sleeve / sizes[m.family], version.member_cap) * (0.5 if m.status == "probation" else 1.0)
            for m in version.members}


def has_plugin(version: PortfolioVersion) -> bool:
    refs = [m.strategy_ref for m in version.members] + ([version.overlay.strategy_ref] if version.overlay else [])
    return any(not r.startswith("template:") for r in refs)


def _targets(strategy_ref: str, parameters: dict, bars: pd.DataFrame, paths,
             sandbox: SandboxConfig | None = None) -> np.ndarray:
    spec = StrategySpec(strategy_ref=strategy_ref, parameters=parameters)
    if spec.kind != "template" and (sandbox is None or sandbox.isolation != "docker"):
        raise BlendError(f"{strategy_ref}: plugin members run only in the docker sandbox (backtests; not paper)")
    t = compute_targets(spec, bars, paths, sandbox).targets
    return pd.Series(t).ffill().fillna(0.0).to_numpy(float)


def blend(version: PortfolioVersion, bars: pd.DataFrame, paths=None, sandbox: SandboxConfig | None = None) -> Blend:
    bars = bars.reset_index(drop=True)
    shares = member_shares(version)
    members = {m.entry_id: _targets(m.strategy_ref, m.parameters, bars, paths, sandbox) for m in version.members}
    families: dict[str, np.ndarray] = {}
    for m in version.members:
        families[m.family] = families.get(m.family, np.zeros(len(bars))) + shares[m.entry_id] * members[m.entry_id]
    pre = np.sum(list(families.values()), axis=0)
    overlay = (np.ones(len(bars)) if version.overlay is None
               else _targets(version.overlay.strategy_ref, version.overlay.parameters, bars, paths, sandbox))
    return Blend(members, families, pre, overlay, np.clip(pre * overlay, 0.0, 1.0))
