from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_research.data.snapshot import freeze_dataset
from quant_research.data.validate import validate_bars
from quant_research.experiments.store import ExperimentStore
from quant_research.paths import get_paths
from quant_research.validation.splits import create_split

HOUR = 3600
T0 = 1704067200  # 2024-01-01T00:00:00Z


def make_raw(n: int, step: int = HOUR, start: int = T0, seed: int = 0, drift: float = 0.0002,
             vol: float = 0.01) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(drift + vol * rng.standard_normal(n)))
    open_ = np.concatenate([[100.0], close[:-1]])
    high = np.maximum(open_, close) * (1 + 0.002 * rng.random(n))
    low = np.minimum(open_, close) * (1 - 0.002 * rng.random(n))
    starts = start + step * np.arange(n)
    return pd.DataFrame({"start": starts, "open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.random(n) * 10, "source_version": "test",
                         "fetched_at": float(starts[-1] + 10 * step)})


LENIENT = {"min_primary": -100.0, "min_dsr": None, "dsr_waiver": "test", "min_round_trips": 0,
           "max_drawdown": -1.0, "require_costs_x2": False, "require_delay": False}


def make_mandate(dataset, acceptance: dict | None = None, **kw):
    from quant_research.controller.budget import Budget
    from quant_research.research.mandate import Mandate

    manifest, split = dataset
    body = {"question": "Does trend following work on BTC hourly?", "dataset_id": manifest["dataset_id"],
            "split_id": split.split_id, "engine_config": {}, "policy": {},
            "evaluation_design": {"kind": "single_validation"}, "primary_metric": "sharpe",
            "acceptance": {**LENIENT, **(acceptance or {})},
            "budget": Budget(max_rounds=3, max_candidates=10, patience_rounds=5),
            "holdout": {"max_viewings": 1, "min_primary": 0.0, "min_total_return": 0.0}}
    return Mandate.model_validate({**body, **kw})


@pytest.fixture
def paths(tmp_path):
    return get_paths(tmp_path / "var")


@pytest.fixture
def store(paths):
    s = ExperimentStore(paths.db)
    yield s
    s.close()


@pytest.fixture
def dataset(paths):
    raw = make_raw(3000, seed=1)
    bars, rep = validate_bars(raw, "coinbase", "BTC-USD", "ONE_HOUR", T0, T0 + 3000 * HOUR)
    manifest = freeze_dataset(paths, bars, rep)
    split = create_split(paths, manifest["dataset_id"])
    return manifest, split
