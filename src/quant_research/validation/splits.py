"""Time-ordered dev / validation / holdout splits with physically separated holdout data.

Research code paths only ever read `splits/<id>/research.parquet`, which stops at the holdout
boundary. Full data including the holdout segment is copied to `holdout/<id>/` and is read only
through `quant_research.validation.holdout`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel

from quant_research.data.bars import to_epoch
from quant_research.data.snapshot import load_dataset
from quant_research.hashing import hash_obj
from quant_research.paths import Paths
from quant_research.timeutil import granularity_seconds, to_iso

ResearchSegment = Literal["dev", "validation"]
Segment = Literal["dev", "validation", "holdout"]


class SplitError(RuntimeError):
    pass


class SplitConfig(BaseModel):
    split_id: str
    dataset_id: str
    data_hash: str
    granularity: str
    dev: tuple[int, int]
    validation: tuple[int, int]
    holdout: tuple[int, int]

    def window(self, segment: Segment) -> tuple[int, int]:
        return getattr(self, segment)

    def describe(self) -> str:
        return " | ".join(f"{s}: {to_iso(a)} -> {to_iso(b)}" for s, (a, b) in
                          (("dev", self.dev), ("validation", self.validation), ("holdout", self.holdout)))


@dataclass
class SegmentData:
    bars: pd.DataFrame  # history from dataset start through the end of the segment (warm-up allowed)
    window: tuple[int, int]  # [start, end) bar-open range that is traded and scored
    segment: str


def create_split(
    paths: Paths,
    dataset_id: str,
    dev_end: int | None = None,
    validation_end: int | None = None,
    fractions: tuple[float, float] = (0.6, 0.2),
) -> SplitConfig:
    bars, manifest = load_dataset(paths, dataset_id)
    step = granularity_seconds(manifest["granularity"])
    opens = to_epoch(bars["bar_open_time"])
    first, end = int(opens[0]), int(opens[-1]) + step
    if dev_end is None or validation_end is None:
        n = len(opens)
        dev_end = dev_end or int(opens[int(n * fractions[0])])
        validation_end = validation_end or int(opens[int(n * (fractions[0] + fractions[1]))])
    dev_end -= dev_end % step
    validation_end -= validation_end % step
    if not first < dev_end < validation_end < end:
        raise SplitError(f"need dataset_start < dev_end < validation_end < dataset_end; got "
                         f"{to_iso(first)} {to_iso(dev_end)} {to_iso(validation_end)} {to_iso(end)}")
    body = {"dataset_id": dataset_id, "data_hash": manifest["data_hash"],
            "granularity": manifest["granularity"], "dev": (first, dev_end),
            "validation": (dev_end, validation_end), "holdout": (validation_end, end)}
    split = SplitConfig(split_id="split-" + hash_obj(body)[:12], **body)

    split_dir = paths.splits / split.split_id
    holdout_dir = paths.holdout / split.split_id
    if (split_dir / "split.json").exists():
        return load_split(paths, split.split_id)
    split_dir.mkdir(parents=True, exist_ok=True)
    holdout_dir.mkdir(parents=True, exist_ok=True)
    bars[opens < validation_end].to_parquet(split_dir / "research.parquet", index=False)
    bars.to_parquet(holdout_dir / "full.parquet", index=False)
    (split_dir / "split.json").write_text(split.model_dump_json(indent=2))
    return split


def load_split(paths: Paths, split_id: str) -> SplitConfig:
    path = paths.splits / split_id / "split.json"
    if not path.exists():
        raise SplitError(f"unknown split {split_id}")
    return SplitConfig.model_validate_json(path.read_text())


def list_splits(paths: Paths) -> list[SplitConfig]:
    return [SplitConfig.model_validate(json.loads(p.read_text()))
            for p in sorted(paths.splits.glob("*/split.json"))]


def load_research_segment(paths: Paths, split_id: str, segment: ResearchSegment) -> SegmentData:
    if segment not in ("dev", "validation"):
        raise SplitError(f"segment {segment!r} is not available to research tools")
    split = load_split(paths, split_id)
    bars = pd.read_parquet(paths.splits / split_id / "research.parquet")
    start, end = split.window(segment)
    opens = to_epoch(bars["bar_open_time"])
    if len(opens) and opens.max() >= split.holdout[0]:
        raise SplitError("research file contains holdout bars; split is corrupted")
    return SegmentData(bars[opens < end].reset_index(drop=True), (start, end), segment)


def agent_data_path(paths: Paths, split_id: str) -> Path:
    """Dev-only copy of the research data for coding agents; validation stays with the controller."""
    path = paths.splits / split_id / "agent_dev.parquet"
    if not path.exists():
        dev = load_research_segment(paths, split_id, "dev")
        tmp = path.with_suffix(".tmp")
        dev.bars.to_parquet(tmp, index=False)
        tmp.replace(path)
    return path


def slice_window(bars: pd.DataFrame, window: tuple[int, int]) -> np.ndarray:
    opens = to_epoch(bars["bar_open_time"])
    return (opens >= window[0]) & (opens < window[1])
