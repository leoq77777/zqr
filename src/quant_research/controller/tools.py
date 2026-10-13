"""CLI-facing agent tools: thin clients of the session's tool gateway (quant_research.gateway)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from quant_research.controller.controller import SessionError
from quant_research.experiments.store import ExperimentStore
from quant_research.gateway import GatewayError, ToolGateway
from quant_research.paths import Paths


def gateway(paths: Paths, store: ExperimentStore, sid: str) -> ToolGateway:
    try:
        return ToolGateway(paths, store, sid)
    except GatewayError as exc:
        raise SessionError(str(exc)) from None


def tool_backtest(paths: Paths, store: ExperimentStore, sid: str, spec_path: Path, segment: str,
                  key: str | None = None) -> dict[str, Any]:
    """Legacy spec-file form of submit_experiment: {"template"|"plugin": ..., "parameters": {...}, "plan_id"?}."""
    if segment != "dev":
        raise PermissionError("agent tools may only use the dev segment; submit candidates and the controller "
                              "evaluates them on validation")
    spec = json.loads(Path(spec_path).read_text())
    artifact = spec.get("template") or spec.get("plugin")
    if not artifact:
        raise SessionError("spec needs 'template' or 'plugin'")
    if spec.get("plugin"):
        artifact = str((Path(spec_path).parent / spec["plugin"]).resolve())
    return tool_submit(paths, store, sid, artifact, spec.get("parameters", {}), spec.get("plan_id"),
                       key or f"spec-{uuid.uuid4().hex[:12]}")


def tool_submit(paths: Paths, store: ExperimentStore, sid: str, artifact: str, parameters: dict[str, Any],
                plan_id: str | None, key: str) -> dict[str, Any]:
    out = gateway(paths, store, sid).submit_experiment(artifact, parameters, plan_id, key)
    if out["status"] == "blocked":
        raise SessionError(out["error"])
    return out


def tool_history(paths: Paths, store: ExperimentStore, sid: str) -> dict[str, Any]:
    return gateway(paths, store, sid).history()
