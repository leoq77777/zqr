"""Experiment Gatekeeper: every pre-run rule in one place, evaluated before any computation.

Outcomes:
- ok: run it.
- blocked: refused for permission/budget/stop reasons; recorded but not counted as a trial.
- rejected: the specification itself is invalid (bad parameters, foreign split, unacknowledged gaps); counted.
- duplicate: identical identity already completed; the existing result is reused and counted as a trial.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from quant_research import ENGINE_VERSION
from quant_research.data.snapshot import load_manifest
from quant_research.experiments.store import ExperimentStore
from quant_research.paths import Paths
from quant_research.strategy.plugin import plugin_path
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.strategy.spec import StrategyError, StrategySpec
from quant_research.validation.splits import SplitError, load_split

Caller = Literal["research", "agent_tool", "controller", "holdout"]
GateStatus = Literal["ok", "blocked", "rejected", "duplicate"]


class GateContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    caller: Caller = "research"
    session_id: str | None = None
    max_session_experiments: int | None = None
    max_trials_per_strategy: int | None = None
    allow_weak_isolation: bool = True  # local CLI users run their own trusted code
    stop_file: Path | None = None
    reuse_duplicates: bool = True


class GateDecision(BaseModel):
    status: GateStatus
    reasons: list[str] = Field(default_factory=list)
    spec: StrategySpec | None = None
    window: tuple[int, int] | None = None
    duplicate_of: str | None = None

    @property
    def allowed(self) -> bool:
        return self.status == "ok"


def gaps_in_range(manifest: dict, start: int, end: int) -> list[dict]:
    return [g for g in manifest.get("gaps", []) if g["start"] < end and g["end"] > start]


def check(
    paths: Paths,
    store: ExperimentStore,
    request,  # ExperimentRequest; untyped to avoid an import cycle with runner
    ctx: GateContext,
    sandbox: SandboxConfig | None = None,
    window: tuple[int, int] | None = None,
    segment: str | None = None,
) -> GateDecision:
    segment = segment or request.segment

    # 1. stop / permissions / budget: blocked, not a trial
    if ctx.stop_file is not None and ctx.stop_file.exists():
        return GateDecision(status="blocked", reasons=["session stop requested"])
    if segment == "holdout" and ctx.caller != "holdout":
        return GateDecision(status="blocked", reasons=["holdout data is only available to the holdout entry point"])
    if ctx.caller == "agent_tool" and segment != "dev":
        return GateDecision(status="blocked", reasons=[f"segment {segment!r} not allowed for agent tools; validation "
                                                       "is reserved for the controller's acceptance evaluation"])
    if ctx.caller == "controller" and segment not in ("dev", "validation"):
        return GateDecision(status="blocked", reasons=[f"segment {segment!r} not allowed for {ctx.caller}"])
    if ctx.session_id and ctx.max_session_experiments is not None:
        used = store.session_usage_count(ctx.session_id)
        if used >= ctx.max_session_experiments:
            return GateDecision(status="blocked",
                                reasons=[f"session experiment budget exhausted ({used}/{ctx.max_session_experiments})"])

    # 2. specification validity: rejected
    try:
        spec = request.spec.normalized()
        if spec.kind == "plugin":
            plugin_path(paths, spec.name)
    except (ValidationError, StrategyError, KeyError) as exc:
        return GateDecision(status="rejected", reasons=[f"invalid strategy/parameters: {exc}"])
    if spec.kind == "plugin" and (sandbox or SandboxConfig()).isolation != "docker" and not ctx.allow_weak_isolation:
        return GateDecision(status="blocked", spec=spec,
                            reasons=["plugin strategies require docker isolation in this context"])
    try:
        split = load_split(paths, request.split_id)
    except SplitError as exc:
        return GateDecision(status="rejected", spec=spec, reasons=[str(exc)])
    if split.dataset_id != request.dataset_id:
        return GateDecision(status="rejected", spec=spec,
                            reasons=[f"split {split.split_id} belongs to {split.dataset_id}, not {request.dataset_id}"])
    window = window or split.window(request.segment)
    manifest = load_manifest(paths, request.dataset_id)
    if manifest["has_unfilled_gaps"] and not request.allow_gaps:
        gaps = gaps_in_range(manifest, split.dev[0], window[1])
        if gaps:
            return GateDecision(status="rejected", spec=spec, window=window,
                                reasons=[f"{len(gaps)} unfilled data gap(s) inside the used history; re-freeze with "
                                         "an explicit fill policy or pass allow_gaps"])

    # 3. trial limits: blocked
    if ctx.max_trials_per_strategy is not None and ctx.caller != "holdout":
        n = store.strategy_trial_count(spec.strategy_hash(), ctx.session_id, request.split_id)
        if n >= ctx.max_trials_per_strategy:
            return GateDecision(status="blocked", spec=spec, window=window,
                                reasons=[f"trial cap for this strategy reached ({n}/{ctx.max_trials_per_strategy}); "
                                         "propose a different hypothesis"])

    # 4. duplicate identity: reuse
    if ctx.reuse_duplicates:
        prior = store.find_completed(spec.candidate_hash(), manifest["data_hash"], request.split_id, window,
                                     request.engine_config.config_id(), ENGINE_VERSION, request.policy.policy_id())
        if prior is not None:
            return GateDecision(status="duplicate", spec=spec, window=window, duplicate_of=prior["id"],
                                reasons=[f"identical experiment already completed as {prior['id']}"])
    return GateDecision(status="ok", spec=spec, window=window)
