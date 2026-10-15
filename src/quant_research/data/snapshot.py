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


def extend_dataset(
    paths: Paths,
    base_id: str,
    prefix: pd.DataFrame,
    prefix_report: ValidationReport,
    raw_dir: Path | None = None,
) -> dict[str, Any]:
    """Freeze `prefix` followed by the base dataset's bars, copied verbatim (appendix A13.4).

    The prefix must end exactly one bar before the base's first bar, with the same venue, instrument, granularity
    and fill policy; the base's rows keep their content hash inside the new dataset.
    """
    from quant_research.timeutil import granularity_seconds

    base, manifest = load_dataset(paths, base_id)
    if manifest.get("synthetic") or manifest.get("venue") == "lab":
        raise SnapshotError(f"{base_id} is synthetic; only venue datasets are extended")
    if prefix.empty:
        raise SnapshotError("refusing to extend with an empty prefix")
    rep = prefix_report
    base_rep = ValidationReport.model_validate(manifest["validation"])
    for field in ("venue", "instrument", "granularity", "fill_policy"):
        if getattr(rep, field) != getattr(base_rep, field):
            raise SnapshotError(f"prefix {field} {getattr(rep, field)!r} differs from the base's "
                                f"{getattr(base_rep, field)!r}")
    step = granularity_seconds(manifest["granularity"])
    first = int(to_epoch(base["bar_open_time"])[0])
    opens = to_epoch(prefix["bar_open_time"])
    if int(opens.max()) >= first:
        raise SnapshotError("prefix overlaps the base dataset")
    if int(opens[-1]) + step != first or rep.trailing_missing_bars:
        raise SnapshotError(f"prefix leaves a gap before the base's first bar ({rep.trailing_missing_bars} trailing "
                            f"missing bars, last prefix bar {int(opens[-1])}, base starts {first})")
    times = ("bar_open_time", "bar_close_time", "available_at", "fetched_at")
    parts = [df[BAR_COLUMNS].assign(**{c: pd.to_datetime(df[c], utc=True).astype("datetime64[ns, UTC]")
                                       for c in times}) for df in (prefix, base)]
    bars = pd.concat(parts, ignore_index=True)
    if content_hash(bars.iloc[len(prefix):].reset_index(drop=True)) != manifest["data_hash"]:
        raise SnapshotError("base rows changed while extending")

    present = (rep.rows - rep.filled_rows) + (base_rep.rows - base_rep.filled_rows)
    expected = rep.expected_rows + base_rep.expected_rows
    invalid = dict(base_rep.invalid_dropped)
    for k, v in rep.invalid_dropped.items():
        invalid[k] = invalid.get(k, 0) + v
    combined = ValidationReport(
        venue=rep.venue, instrument=rep.instrument, granularity=rep.granularity,
        requested_start=rep.requested_start, requested_end=base_rep.requested_end,
        **{k: getattr(rep, k) + getattr(base_rep, k) for k in (
            "raw_rows", "out_of_range", "duplicates", "duplicate_conflicts", "incomplete_dropped",
            "misaligned_dropped", "expected_rows", "internal_gap_bars", "filled_rows")},
        invalid_dropped=invalid, rows=len(bars), coverage=round(present / expected, 6) if expected else 0.0,
        actual_first=rep.actual_first, actual_last=base_rep.actual_last,
        leading_missing_bars=rep.leading_missing_bars, trailing_missing_bars=base_rep.trailing_missing_bars,
        gaps=rep.gaps + base_rep.gaps, fill_policy=rep.fill_policy)
    last_close = float(prefix["close"].iloc[-1])
    first_open = float(base["open"].iloc[0])
    junction = {"prefix_last_open": int(opens[-1]), "base_first_open": first, "prefix_last_close": last_close,
                "base_first_open_price": first_open, "relative_jump": round(first_open / last_close - 1, 6)}
    return freeze_dataset(paths, bars, combined, raw_dir, extra={
        "extends": base_id, "base_data_hash": manifest["data_hash"], "junction": junction})


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
