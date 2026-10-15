"""A registered hypothesis (appendix A6): a written prior with citations and one to three variants."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from quant_research.evaluation.split_stats import MAX_REGISTERED
from quant_research.strategy.spec import StrategySpec


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Citation(_Model):
    kind: Literal["published", "experiment"]
    ref: str = Field(min_length=1)  # a reference to published work, or an experiment id on the hypothesis split


class Variant(_Model):
    strategy_ref: str
    parameters: dict[str, Any] = Field(default_factory=dict)

    def spec(self) -> StrategySpec:
        return StrategySpec(strategy_ref=self.strategy_ref, parameters=self.parameters).normalized()


class Hypothesis(_Model):
    product: str
    family: str
    prior: str = Field(min_length=1, max_length=2000)
    citations: tuple[Citation, ...] = Field(min_length=1)
    variants: tuple[Variant, ...] = Field(min_length=1, max_length=MAX_REGISTERED)
    campaign_id: str | None = None
