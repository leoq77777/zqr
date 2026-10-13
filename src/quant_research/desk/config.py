"""Desk configuration (`desk/config.json`, written by `zqr desk setup`, safe to hand-edit) and check results."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, NonNegativeFloat, PositiveFloat, PositiveInt

from quant_research.paths import Paths


class NetworkConfig(BaseModel):
    """How the egress proxy (in Docker) reaches the tool gateway (on the host, in WSL, or in the desk container)."""

    mode: str = "docker_desktop"
    gateway_bind: str = "127.0.0.1"
    forward_host: str = "host.docker.internal"
    control_network: str | None = None


def container_network() -> NetworkConfig | None:
    """Release layout: the gateway listens in the desk container; each run's proxy joins the control network.
    Computed per use because the desk container's address changes when it is recreated."""
    from quant_research.runtime import control_network, own_address

    net = control_network()
    if not net:
        return None
    return NetworkConfig(mode="container", gateway_bind="0.0.0.0", forward_host=own_address(), control_network=net)


def effective_network(cfg: DeskConfig) -> NetworkConfig:
    return container_network() or cfg.network


class DeskConfig(BaseModel):
    products: list[str] = Field(default_factory=lambda: ["BTC-USD", "ETH-USD"])
    mandates: dict[str, str] = Field(default_factory=dict)  # product -> mandate_id
    daily_cost_cap_usd: NonNegativeFloat = 20.0
    session_wall_seconds: PositiveFloat = 1800
    backoff_base_seconds: PositiveFloat = 300
    backoff_max_seconds: PositiveFloat = 3600
    poll_seconds: PositiveFloat = 30
    report_hour_utc: int = Field(0, ge=0, le=23)
    report_minute_utc: int = Field(5, ge=0, le=59)
    model: str | None = None
    cny_per_usd: PositiveFloat = 7.1
    agent_memory: str = "2g"
    agent_cpus: str = "2"
    max_sessions_per_day: PositiveInt = 200
    research_enabled: bool = True
    network: NetworkConfig = Field(default_factory=NetworkConfig)


def config_path(paths: Paths) -> Path:
    return paths.desk / "config.json"


def check_path(paths: Paths) -> Path:
    return paths.desk / "check.json"


def load_config(paths: Paths) -> DeskConfig:
    p = config_path(paths)
    return DeskConfig.model_validate_json(p.read_text()) if p.exists() else DeskConfig()


def save_config(paths: Paths, cfg: DeskConfig) -> None:
    paths.desk.mkdir(parents=True, exist_ok=True)
    config_path(paths).write_text(cfg.model_dump_json(indent=2))


def load_check(paths: Paths) -> dict:
    p = check_path(paths)
    return json.loads(p.read_text()) if p.exists() else {}
