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
from quant_research.data.snapshot import load_dataset, load_manifest
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


def create_split_like(paths: Paths, dataset_id: str, like_id: str) -> SplitConfig:
    """A split of `dataset_id` with the validation and holdout windows of split `like_id` (appendix A13.4)."""
    like = load_split(paths, like_id)
    manifest = load_manifest(paths, dataset_id)
    end = manifest["actual_last_open"] + granularity_seconds(manifest["granularity"])
    if manifest["granularity"] != like.granularity or end != like.holdout[1]:
        raise SplitError(f"{dataset_id} ends at {to_iso(end)} ({manifest['granularity']}); {like_id}'s holdout ends "
                         f"at {to_iso(like.holdout[1])} ({like.granularity}): the windows would differ")
    return create_split(paths, dataset_id, like.dev[1], like.validation[1])


def dataset_lineage(paths: Paths, dataset_id: str) -> list[str]:
    """The dataset and the datasets it extends, nearest first."""
    out = [dataset_id]
    while (base := load_manifest(paths, out[-1]).get("extends")) and base not in out:
        out.append(base)
    return out


def link_split(paths: Paths, store, split_id: str, parent_id: str, note: str) -> dict:
    """Record `parent_id` as the parent of `split_id` (appendix A13.4); a child of a closed split is closed."""
    if not note.strip():
        raise SplitError("a split lineage needs a note")
    child, parent = load_split(paths, split_id), load_split(paths, parent_id)
    cm, pm = load_manifest(paths, child.dataset_id), load_manifest(paths, parent.dataset_id)
    problems = []
    if (cm["instrument"], child.granularity) != (pm["instrument"], parent.granularity):
        problems.append(f"product {cm['instrument']} {child.granularity} vs {pm['instrument']} {parent.granularity}")
    if child.validation != parent.validation or child.holdout != parent.holdout:
        problems.append("validation or holdout windows differ")
    if not (child.dev[0] <= parent.dev[0] and child.dev[1] == parent.dev[1]):
        problems.append("the child's dev does not contain the parent's")
    if parent.dataset_id not in dataset_lineage(paths, child.dataset_id):
        problems.append(f"{child.dataset_id} does not extend {parent.dataset_id}")
    if problems:
        raise SplitError(f"{split_id} cannot inherit from {parent_id}: " + "; ".join(problems))
    try:
        lineage = store.record_lineage(split_id, parent_id, note)
    except ValueError as exc:
        raise SplitError(str(exc)) from None
    closure = store.holdout_closure(split_id)
    if closure is None and lineage["parent_id"] == parent_id:
        parent_closure = store.holdout_closure(parent_id)
        if parent_closure is not None:
            closure = store.close_holdout(split_id, f"inherits the closure of {parent_id}: {parent_closure['note']}")
    return {"lineage": lineage, "closure": closure}


def load_split(paths: Paths, split_id: str) -> SplitConfig:
    path = paths.splits / split_id / "split.json"
    if not path.exists():
        raise SplitError(f"unknown split {split_id}")
    return SplitConfig.model_validate_json(path.read_text())


def list_splits(paths: Paths) -> list[SplitConfig]:
    return [SplitConfig.model_validate(json.loads(p.read_text()))
            for p in sorted(paths.splits.glob("*/split.json"))]


def holdout_cutoff(paths: Paths, instrument: str, granularity: str) -> int | None:
    """Earliest protected holdout start for a product and granularity: tools outside `run_holdout` stop there.

    A holdout that a later split folds into its research data (appendix A11) is no longer protected.
    """
    splits = [s for s in list_splits(paths)
              if s.granularity == granularity and load_manifest(paths, s.dataset_id)["instrument"] == instrument]
    research = [(s.dev[0], s.validation[1]) for s in splits]
    starts = [s.holdout[0] for s in splits
              if not any(a <= s.holdout[0] and s.holdout[1] <= b for a, b in research)]
    return min(starts, default=None)


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
