"""Immutable Parquet dataset snapshots with a manifest and content hash."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any

import pandas as pd

from quant_research.data.bars import BAR_COLUMNS, to_epoch
from quant_research.data.validate import ValidationReport
from quant_research.hashing import hash_obj, sha256_file, sha256_text
from quant_research.paths import Paths

HASH_COLUMNS = ["bar_open_time", "bar_close_time", "available_at", "open", "high", "low", "close",
                "volume", "filled"]


class SnapshotError(RuntimeError):
    pass


def content_hash(bars: pd.DataFrame) -> str:
    """Hash of the economically relevant content, stable across Parquet writer versions."""
    canon = pd.DataFrame(
        {
            "bar_open_time": to_epoch(bars["bar_open_time"]),
            "bar_close_time": to_epoch(bars["bar_close_time"]),
            "available_at": to_epoch(bars["available_at"]),
            **{c: bars[c].astype(float).map(repr) for c in ("open", "high", "low", "close", "volume")},
            "filled": bars["filled"].astype(bool).astype(int),
        }
    )[HASH_COLUMNS]
    buf = io.StringIO()
    canon.to_csv(buf, index=False, lineterminator="\n")
    return sha256_text(buf.getvalue())


def raw_sources_hash(raw_dir: Path) -> str:
    files = sorted(raw_dir.glob("*_*.json"))
    return hash_obj([[f.name, sha256_file(f)] for f in files])


def freeze_dataset(
    paths: Paths,
    bars: pd.DataFrame,
    report: ValidationReport,
    raw_dir: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if bars.empty:
        raise SnapshotError("refusing to freeze an empty dataset")
    data_hash = content_hash(bars)
    dataset_id = f"{report.venue}-{report.instrument}-{report.granularity}-{data_hash[:12]}".lower()
    out_dir = paths.snapshots / dataset_id
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
        if existing["data_hash"] != data_hash:
            raise SnapshotError(f"{dataset_id} exists with different content")
        return existing

    out_dir.mkdir(parents=True)
    parquet_path = out_dir / "bars.parquet"
    bars[BAR_COLUMNS].to_parquet(parquet_path, index=False)
    manifest = {
        "dataset_id": dataset_id,
        "data_hash": data_hash,
        "venue": report.venue,
        "instrument": report.instrument,
        "granularity": report.granularity,
        "requested_start": report.requested_start,
        "requested_end": report.requested_end,
        "actual_first_open": report.actual_first,
        "actual_last_open": report.actual_last,
        "rows": len(bars),
        "coverage": report.coverage,
        "gaps": [g.model_dump() for g in report.gaps],
        "internal_gap_bars": report.internal_gap_bars,
        "fill_policy": report.fill_policy,
        "filled_rows": report.filled_rows,
        "has_unfilled_gaps": report.has_unfilled_gaps,
        "validation": report.model_dump(),
        "raw_sources_hash": raw_sources_hash(raw_dir) if raw_dir else None,
        "parquet_sha256": sha256_file(parquet_path),
        "created_at": time.time(),
        **(extra or {}),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    for p in (parquet_path, manifest_path):
        p.chmod(0o444)
    return manifest


def load_manifest(paths: Paths, dataset_id: str) -> dict[str, Any]:
    path = paths.snapshots / dataset_id / "manifest.json"
    if not path.exists():
        raise SnapshotError(f"unknown dataset {dataset_id}")
    return json.loads(path.read_text())


def load_dataset(paths: Paths, dataset_id: str, verify: bool = True) -> tuple[pd.DataFrame, dict[str, Any]]:
    manifest = load_manifest(paths, dataset_id)
    bars = pd.read_parquet(paths.snapshots / dataset_id / "bars.parquet")
    if verify and content_hash(bars) != manifest["data_hash"]:
        raise SnapshotError(f"{dataset_id} content hash mismatch; snapshot was modified")
    return bars, manifest


def list_datasets(paths: Paths) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(paths.snapshots.glob("*/manifest.json"))]
