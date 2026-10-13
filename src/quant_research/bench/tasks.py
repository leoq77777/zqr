"""Benchmark task definitions: v0.1 briefs and budgets plus the v0.2 data/segment/fault mapping."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quant_research.runtime import benchmark_dir

SCREEN = ("B02", "B03", "B05", "B07")
CONTRACT_V02 = "v0.2/strategy_contract.md"
TASK_DATA = {"B02": "synthetic_development", "B03": "synthetic_development", "B05": "synthetic_unknown",
             "B07": "synthetic_development", "B08": "synthetic_development"}
AGENT_SEGMENTS = {"B05": ("dev", "validation")}
FAULTS = {"B08": ["timeout_after_commit"]}


@dataclass(frozen=True)
class BenchTask:
    id: str
    title: str
    brief: str
    visible_fixtures: list[str]
    budget: dict[str, Any]
    checks: list[str]
    dataset: str | None
    agent_segments: tuple[str, ...] = ("dev",)
    faults: list[str] = field(default_factory=list)

    @property
    def wall_seconds(self) -> float:
        return float(self.budget.get("max_wall_seconds", 900))


def load_tasks(root: Path | None = None) -> dict[str, BenchTask]:
    data = json.loads(((root or benchmark_dir()) / "tasks.json").read_text())
    out = {}
    for t in data["tasks"]:
        out[t["id"]] = BenchTask(id=t["id"], title=t["title"], brief=t["agent_brief"],
                                 visible_fixtures=t["visible_fixtures"], budget=t["budget"],
                                 checks=t["evaluator_checks"], dataset=TASK_DATA.get(t["id"]),
                                 agent_segments=AGENT_SEGMENTS.get(t["id"], ("dev",)),
                                 faults=FAULTS.get(t["id"], []))
    return out
