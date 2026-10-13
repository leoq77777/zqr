"""Ingest benchmark fixtures as `lab` snapshots with a private holdout continuation (never mounted)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from quant_research.bench.tasks import benchmark_dir
from quant_research.data.snapshot import freeze_dataset
from quant_research.data.validate import validate_bars
from quant_research.paths import Paths
from quant_research.validation.splits import SplitConfig, create_split

HOLDOUT_BARS = 512
STEP = 3600
GENERATORS = {"synthetic_development": "development_generator", "synthetic_unknown": "unknown_generator"}


class FixtureError(RuntimeError):
    pass


def load_fixture(name: str, root: Path | None = None) -> tuple[pd.DataFrame, dict]:
    root = root or benchmark_dir()
    path = root / "fixtures" / f"{name}.jsonl"
    manifest = json.loads((root / "fixtures" / f"{name}.manifest.json").read_text())
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
        raise FixtureError(f"{path.name} does not match its manifest hash")
    return pd.read_json(path, lines=True), manifest


def holdout_continuation(bars: pd.DataFrame, n: int, phi: float, seed: int) -> pd.DataFrame:
    """Continue the series with the fixture's generating process (AR(1) log returns, fitted volatility)."""
    rng = np.random.default_rng(seed)
    r = np.diff(np.log(bars["close"].to_numpy()))
    sigma = float(np.std(r[1:] - phi * r[:-1]))
    mu = float(np.mean(r))
    out, prev, close = [], float(r[-1]), float(bars["close"].iloc[-1])
    start = int(bars["start"].iloc[-1]) + STEP
    for i in range(n):
        ret = mu + phi * (prev - mu) + sigma * rng.standard_normal()
        open_, close = close, close * float(np.exp(ret))
        wick = np.abs(rng.standard_normal(2)) * sigma * 0.5
        out.append({"start": start + i * STEP, "open": open_, "high": max(open_, close) * float(np.exp(wick[0])),
                    "low": min(open_, close) * float(np.exp(-wick[1])), "close": close,
                    "volume": float(bars["volume"].median() * np.exp(0.3 * rng.standard_normal()))})
        prev = ret
    return pd.DataFrame(out)


def ingest(paths: Paths, name: str, root: Path | None = None) -> tuple[dict, SplitConfig]:
    """Snapshot = visible fixture rows + private continuation; split at the fixture's dev/validation bounds."""
    root = root or benchmark_dir()
    bars, manifest = load_fixture(name, root)
    gen = json.loads((root / "evaluator_private" / "generation.json").read_text())[GENERATORS[name]]
    private_seed = int(gen["seed"]) * 1009 + 7  # not the public generator seed; never written to a workspace
    full = pd.concat([bars, holdout_continuation(bars, HOLDOUT_BARS, float(gen["phi"]), private_seed)],
                     ignore_index=True)
    full["source_version"] = f"benchmark-fixture:{name}"
    full["fetched_at"] = float(full["start"].iloc[-1] + 2 * STEP)
    first, end = int(full["start"].iloc[0]), int(full["start"].iloc[-1]) + STEP
    clean, report = validate_bars(full, "lab", name.upper().replace("_", "-"), "ONE_HOUR", first, end)
    snap = freeze_dataset(paths, clean, report, extra={"synthetic": True, "benchmark_fixture": name,
                                                        "fixture_sha256": manifest["sha256"]})
    dev_end = int(bars["start"].iloc[manifest["development"][1]])
    validation_end = int(bars["start"].iloc[-1]) + STEP
    return snap, create_split(paths, snap["dataset_id"], dev_end, validation_end)
