"""Daily return archive (appendix A1): every dev trial reduced to daily returns, one parquet per split.

A trial is what `ExperimentStore.trial_count` counts, on the dev segment. Completed (and duplicate) trials store
their daily returns; failed, rejected and aborted trials are indexed with `no_returns = 1`, each counting once in K.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quant_research.evaluation.split_stats import sharpe_daily
from quant_research.experiments.store import NON_TRIAL_KINDS, NOT_A_TRIAL, ExperimentStore
from quant_research.memory.schema import ensure
from quant_research.paths import Paths

log = logging.getLogger(__name__)
WITH_RETURNS = ("completed", "duplicate")
DEFAULT_CASH = 10_000.0


class ArchiveError(Exception):
    pass


def is_dev_trial(row: dict[str, Any]) -> bool:
    return (row.get("segment") == "dev" and row.get("kind") not in NON_TRIAL_KINDS
            and row.get("status") not in (*NOT_A_TRIAL, "running"))


def daily_returns(equity: pd.DataFrame, initial_cash: float) -> pd.Series:
    """Equity at the close of each UTC day's last bar over the previous day's (the first day over the cash)."""
    day = pd.to_datetime(equity["time"], utc=True).dt.normalize().dt.tz_localize(None)
    e = equity["equity"].astype(float).groupby(day.to_numpy()).last()
    values = e.to_numpy()
    prev = np.concatenate([[initial_cash], values[:-1]])
    return pd.Series(values / prev - 1, index=pd.DatetimeIndex(e.index, name="day"))


def _decoded(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for k in ("engine_config", "metrics", "notes"):
        if isinstance(out.get(k), str):
            try:
                out[k] = json.loads(out[k])
            except json.JSONDecodeError:
                pass
    return out


class ReturnArchive:
    def __init__(self, paths: Paths, store: ExperimentStore):
        self.paths, self.store = paths, ensure(store)
        self.dir = paths.library / "returns"

    def path(self, split_id: str) -> Path:
        return self.dir / f"{split_id}.parquet"

    def load(self, split_id: str) -> pd.DataFrame:
        p = self.path(split_id)
        return pd.read_parquet(p) if p.exists() else pd.DataFrame()

    def index(self, split_id: str) -> list[dict[str, Any]]:
        return self.store.query("SELECT * FROM return_index WHERE split_id=? ORDER BY experiment_id", (split_id,))

    def splits(self) -> list[str]:
        return [r["split_id"] for r in self.store.query("SELECT DISTINCT split_id FROM return_index ORDER BY 1")]

    def _write(self, split_id: str, frame: pd.DataFrame) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path(split_id).with_suffix(".tmp")
        frame[sorted(frame.columns)].to_parquet(tmp)
        os.replace(tmp, self.path(split_id))

    def add(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
        """Archive the dev trials among `rows` not archived yet; returns counts per split."""
        by_split: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in map(_decoded, rows):
            if is_dev_trial(r) and r.get("split_id"):
                by_split[r["split_id"]].append(r)
        counts: dict[str, dict[str, int]] = {}
        for split_id, group in sorted(by_split.items()):
            known = {r["experiment_id"] for r in self.index(split_id)}
            new = sorted((r for r in group if r["id"] not in known), key=lambda r: r["id"])
            c = counts[split_id] = {"added": 0, "no_returns": 0, "missing_equity": 0, "present": len(group) - len(new)}
            if not new:
                continue
            frame, columns, entries = self.load(split_id), {}, []
            for r in new:
                series = None
                if r["status"] in WITH_RETURNS:
                    f = Path(r.get("result_dir") or "") / "equity.parquet"
                    if f.is_file():
                        cash = float((r.get("engine_config") or {}).get("initial_cash", DEFAULT_CASH))
                        series = daily_returns(pd.read_parquet(f, columns=["time", "equity"]), cash)
                    else:
                        c["missing_equity"] += 1
                if series is not None:
                    if len(frame.columns):
                        ref = frame.index
                    elif columns:
                        ref = next(iter(columns.values())).index
                    else:
                        ref = series.index
                    if not series.index.equals(ref):
                        raise ArchiveError(f"{r['id']}: its days differ from the split's archive "
                                           f"({len(series)} vs {len(ref)} days)")
                    columns[r["id"]] = series
                    c["added"] += 1
                else:
                    c["no_returns"] += 1
                metrics = r.get("metrics") or {}
                entries.append((r["id"], split_id, r["kind"], r.get("session_id"),
                                None if series is None else len(series),
                                None if series is None else sharpe_daily(series.to_numpy()),
                                metrics.get("turnover"), int(series is None), time.time()))
            if columns:
                frame = pd.concat([frame, pd.DataFrame(columns)], axis=1) if len(frame.columns) \
                    else pd.DataFrame(columns)
                frame.index.name = "day"
                self._write(split_id, frame)
            for e in entries:
                self.store.execute("INSERT OR IGNORE INTO return_index (experiment_id, split_id, kind, session_id, "
                                   "n_days, sharpe, turnover, no_returns, added_at) VALUES (?,?,?,?,?,?,?,?,?)", e)
        return counts


def backfill(paths: Paths, store: ExperimentStore, split_id: str | None = None) -> dict[str, dict[str, int]]:
    """Archive every dev trial on record (idempotent)."""
    where, args = "segment='dev' AND status NOT IN ('blocked','running')", []
    if split_id:
        where, args = where + " AND split_id=?", [split_id]
    rows = store.query(f"SELECT * FROM experiments WHERE {where} ORDER BY id", args)
    return ReturnArchive(paths, store).add(rows)


def archive_new(paths: Paths, store: ExperimentStore, experiment_ids: list[str]) -> None:
    """Called by the runner after experiments finish; a failure here never fails the experiment."""
    try:
        rows = [r for r in (store.get_experiment(i) for i in experiment_ids) if r]
        ReturnArchive(paths, store).add(rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("return archive not updated for %s: %s (run `zqr memory backfill`)", experiment_ids, exc)
