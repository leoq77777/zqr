"""Research mandate (docs/contracts-v2.md section 6): written by the human before research, immutable afterwards.

Acceptance thresholds have no defaults in code; every mandate states them explicitly.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, model_validator

from quant_research.backtest.engine import EngineConfig
from quant_research.controller.budget import Budget
from quant_research.hashing import hash_obj
from quant_research.portfolio.policy import PortfolioPolicy

PrimaryMetric = Literal["sharpe", "calmar", "total_return"]


class SingleValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["single_validation"] = "single_validation"


class WalkForwardOOS(BaseModel):
    """Rolling re-selection of the plan's parameter grid over dev+validation; the candidate is the procedure."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["walk_forward_oos"] = "walk_forward_oos"
    train_bars: PositiveInt
    test_bars: PositiveInt


EvaluationDesign = Annotated[SingleValidation | WalkForwardOOS, Field(discriminator="kind")]


class AcceptanceCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_primary: float
    min_dsr: float | None  # Deflated Sharpe against all research trials on the split
    dsr_waiver: str | None = None  # required reason when min_dsr is null
    min_round_trips: NonNegativeInt
    max_drawdown: float = Field(le=0)  # reject if drawdown is worse (more negative)
    require_costs_x2: bool  # still profitable with doubled costs
    require_delay: bool  # still profitable with a one-bar delay and adverse fills
    require_beats_policy_matched: bool = False
    require_bootstrap_lower_positive: bool = False

    @model_validator(mode="after")
    def _dsr_or_reason(self):
        if self.min_dsr is None and not self.dsr_waiver:
            raise ValueError("min_dsr is null: state the reason in dsr_waiver")
        return self


class HoldoutPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_viewings: PositiveInt  # holdout runs allowed on the split under this mandate
    min_primary: float  # final pass rule on the holdout
    min_total_return: float


class Mandate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str
    dataset_id: str
    split_id: str
    engine_config: EngineConfig  # base costs; sensitivity cases derive from it (contracts-v2 section 4)
    policy: PortfolioPolicy
    evaluation_design: EvaluationDesign
    primary_metric: PrimaryMetric
    acceptance: AcceptanceCriteria
    budget: Budget
    holdout: HoldoutPolicy
    agent_segments: tuple[Literal["dev", "validation"], ...] = ("dev",)  # segments agent tools may run on
    notes: str = ""

    def mandate_id(self) -> str:
        return "mandate-" + hash_obj(self.model_dump(mode="json"))[:12]


def metric_value(metrics: dict, name: str) -> float:
    return float(metrics.get(name, 0.0) or 0.0)
