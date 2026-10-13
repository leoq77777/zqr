"""Preregistered research plans (docs/contracts-v2.md section 6).

A plan is written before the experiments it governs and never edited; a changed plan supersedes the old one.
Experiments reference a plan; without one they are `unplanned`, outside its artifact/ranges/count `out_of_plan`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator


class ParamRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min: float | None = None
    max: float | None = None
    values: list[Any] | None = None

    @model_validator(mode="after")
    def _one_form(self):
        if self.values is None and self.min is None and self.max is None:
            raise ValueError("a parameter range needs `values` or `min`/`max`")
        return self

    def contains(self, value: Any) -> bool:
        if self.values is not None:
            return value in self.values
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        return (self.min is None or value >= self.min) and (self.max is None or value <= self.max)

    def grid(self) -> list[Any]:
        """Explicit values for walk-forward selection; numeric ranges must list values to be searchable."""
        if self.values is None:
            raise ValueError("walk-forward evaluation needs explicit `values` for every parameter")
        return list(self.values)


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis: str = Field(min_length=10)
    mechanism: str = Field(min_length=5)  # why the effect should exist
    falsification: str = Field(min_length=5)  # what result would reject the hypothesis
    artifact: str  # template name, or a plugin file name in the workspace
    parameter_ranges: dict[str, ParamRange] = Field(default_factory=dict)
    planned_experiments: PositiveInt
    primary_metric: str = "sharpe"
    benchmarks: list[str] = Field(default_factory=lambda: ["cash", "buy_and_hold", "policy_matched"])

    def parameter_grid(self) -> dict[str, list[Any]]:
        return {k: r.grid() for k, r in self.parameter_ranges.items()}


def artifact_name(strategy_ref: str, artifact: str | None) -> str:
    """Template name for templates, the submitted file name for plugins."""
    if strategy_ref.startswith("template:"):
        return strategy_ref.split(":", 1)[1]
    return artifact or strategy_ref


def plan_status(plan: ResearchPlan | None, artifact: str, parameters: dict[str, Any],
                used: int) -> tuple[str, str | None]:
    """(`planned` | `unplanned` | `out_of_plan`, deviation reason)."""
    if plan is None:
        return "unplanned", None
    problems = []
    if artifact.rsplit("/", 1)[-1] != plan.artifact.rsplit("/", 1)[-1]:
        problems.append(f"artifact {artifact!r} is not the planned {plan.artifact!r}")
    for key, value in parameters.items():
        rng = plan.parameter_ranges.get(key)
        if rng is not None and not rng.contains(value):
            problems.append(f"{key}={value!r} outside the planned range")
    if used >= plan.planned_experiments:
        problems.append(f"plan allowed {plan.planned_experiments} experiment(s), {used} already used")
    return ("out_of_plan", "; ".join(problems)) if problems else ("planned", None)
