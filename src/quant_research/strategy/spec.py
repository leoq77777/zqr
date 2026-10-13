from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from quant_research.hashing import hash_obj
from quant_research.strategy.templates import get_template


class StrategyError(ValueError):
    pass


class StrategySpec(BaseModel):
    """`strategy_ref` is `template:<name>` or `plugin:<sha256 of frozen code>`."""

    strategy_ref: str
    parameters: dict[str, Any] = Field(default_factory=dict)

    @property
    def kind(self) -> str:
        return self.strategy_ref.split(":", 1)[0]

    @property
    def name(self) -> str:
        return self.strategy_ref.split(":", 1)[1]

    def normalized(self) -> StrategySpec:
        if self.kind == "template":
            return StrategySpec(strategy_ref=self.strategy_ref,
                                parameters=get_template(self.name).validate(self.parameters))
        if self.kind == "plugin":
            return self
        raise StrategyError(f"bad strategy_ref {self.strategy_ref!r}")

    def identity(self) -> dict[str, Any]:
        ident: dict[str, Any] = {"strategy_ref": self.strategy_ref, "parameters": self.parameters}
        if self.kind == "template":
            ident["template_version"] = get_template(self.name).version
        return ident

    def strategy_hash(self) -> str:
        """Hash of the strategy rule itself (template+version or plugin code), without parameters."""
        if self.kind == "template":
            return hash_obj({"template": self.name, "version": get_template(self.name).version})
        return self.name

    def candidate_hash(self) -> str:
        return hash_obj(self.identity())


def normalize_targets(values: Any, n: int, strict: bool = False) -> np.ndarray:
    """Validate a per-bar series in [0, 1]. `strict` (exposure contract v0.2) rejects NaN instead of zeroing it;
    the legacy target contract keeps NaN -> 0. Values are never clipped beyond floating-point noise."""
    arr = np.asarray(pd.Series(values).to_numpy(), dtype=float)
    if arr.shape != (n,):
        raise StrategyError(f"strategy returned {arr.shape} values for {n} bars")
    if strict and np.isnan(arr).any():
        raise StrategyError(f"exposures contain NaN at {int(np.isnan(arr).sum())} bar(s); return 0 during warmup")
    arr = np.where(np.isnan(arr), 0.0, arr)
    if (arr < -1e-12).any() or (arr > 1 + 1e-12).any():
        raise StrategyError("values must lie in [0, 1] (long/cash spot only)")
    return np.clip(arr, 0.0, 1.0)


def load_spec_file(path: Path, register_plugin=None) -> StrategySpec:
    """JSON file: {"template": name, "parameters": {...}} or {"plugin": "file.py", "parameters": {...}}."""
    data = json.loads(Path(path).read_text())
    params = data.get("parameters", {})
    if "template" in data:
        return StrategySpec(strategy_ref=f"template:{data['template']}", parameters=params).normalized()
    if "plugin" in data:
        if register_plugin is None:
            raise StrategyError("plugin spec requires a plugin registry")
        code = (Path(path).parent / data["plugin"]).resolve()
        return StrategySpec(strategy_ref=f"plugin:{register_plugin(code)}", parameters=params)
    if "strategy_ref" in data:
        return StrategySpec.model_validate(data).normalized()
    raise StrategyError(f"{path}: expected 'template', 'plugin' or 'strategy_ref'")
