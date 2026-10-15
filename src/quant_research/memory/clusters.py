"""Residual clustering and the effective number of trials K (appendix A3).

Average-linkage hierarchical clustering on `sqrt((1 - rho) / 2)`, cut at residual correlations 0.3, 0.5 and 0.7.
Labels are canonical (each cluster is named by its smallest experiment id), so recomputing on the same inputs gives
the same labels. K(rho) = clusters + one for any flat trials + one for any passive trials + one per trial without
returns; K(0.7) is the K used in calculations.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

from quant_research.experiments.store import ExperimentStore, new_id
from quant_research.memory.residuals import PASSIVE_RULE, residuals, split_market_returns
from quant_research.memory.returns import ReturnArchive
from quant_research.memory.schema import ensure
from quant_research.paths import Paths

THRESHOLDS = (0.3, 0.5, 0.7)
K_THRESHOLD = 0.7
FLAT, PASSIVE = "flat", "passive"


def _key(threshold: float) -> str:
    return f"{threshold:.1f}"


@dataclass
class Clustering:
    split_id: str
    input_hash: str
    labels: dict[str, dict[str, str]]  # threshold key -> experiment id -> label
    k: dict[str, int]
    counts: dict[str, int]
    flags: pd.DataFrame = field(repr=False)

    def clusters(self, threshold: float) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for exp_id, label in sorted(self.labels[_key(threshold)].items()):
            out.setdefault(label, []).append(exp_id)
        return out


def input_hash(returns: pd.DataFrame, no_returns: list[str], winsorise: bool) -> str:
    h = hashlib.sha256()
    cols = sorted(returns.columns)
    h.update(json.dumps({"columns": cols, "no_returns": sorted(no_returns), "winsorise": winsorise,
                         "thresholds": THRESHOLDS, "passive_rule": PASSIVE_RULE}).encode())
    h.update(np.ascontiguousarray(returns[cols].to_numpy(float)).tobytes())
    return h.hexdigest()[:16]


def cluster(resid: pd.DataFrame, flags: pd.DataFrame, no_returns: list[str]) -> tuple[dict[str, dict[str, str]],
                                                                                      dict[str, int]]:
    special = {i: FLAT for i in flags.index[flags["flat"]]} | {i: PASSIVE for i in flags.index[flags["passive"]]}
    ids = sorted(c for c in resid.columns if c not in special)
    cuts: dict[str, np.ndarray] = {}
    if len(ids) >= 2:
        rho = np.clip(np.corrcoef(resid[ids].to_numpy(float), rowvar=False), -1.0, 1.0)
        dist = np.sqrt((1.0 - rho) / 2.0)
        np.fill_diagonal(dist, 0.0)
        z = linkage(squareform(dist, checks=False), method="average")
        cuts = {_key(t): fcluster(z, t=np.sqrt((1.0 - t) / 2.0), criterion="distance") for t in THRESHOLDS}
    else:
        cuts = {_key(t): np.ones(len(ids), dtype=int) for t in THRESHOLDS}
    labels: dict[str, dict[str, str]] = {}
    k: dict[str, int] = {}
    for key, raw in cuts.items():
        groups: dict[int, list[str]] = {}
        for exp_id, g in zip(ids, raw, strict=True):
            groups.setdefault(int(g), []).append(exp_id)
        lab = {e: min(members) for members in groups.values() for e in members}
        lab |= special | {e: e for e in no_returns}
        labels[key] = dict(sorted(lab.items()))
        k[key] = len(groups) + (FLAT in special.values()) + (PASSIVE in special.values()) + len(no_returns)
    return labels, k


def compute(paths: Paths, store: ExperimentStore, split_id: str, winsorise: bool = True) -> Clustering:
    arc = ReturnArchive(paths, store)
    returns = arc.load(split_id)
    no_returns = [r["experiment_id"] for r in arc.index(split_id) if r["no_returns"]]
    if len(returns.columns):
        resid, flags = residuals(returns, split_market_returns(paths, split_id), winsorise)
    else:
        resid, flags = returns, pd.DataFrame(columns=["beta", "residual_sd", "flat", "passive"], dtype=bool)
    labels, k = cluster(resid, flags, no_returns)
    counts = {"trials": len(returns.columns) + len(no_returns), "with_returns": len(returns.columns),
              "no_returns": len(no_returns), "flat": int(flags["flat"].sum()) if len(flags) else 0,
              "passive": int(flags["passive"].sum()) if len(flags) else 0}
    return Clustering(split_id, input_hash(returns, no_returns, winsorise), labels, k, counts, flags)


def record(store: ExperimentStore, c: Clustering) -> str:
    """Store a clustering unless the latest one for the split has the same inputs; returns its id."""
    ensure(store)
    last = latest(store, c.split_id)
    if last and last["input_hash"] == c.input_hash:
        return last["id"]
    cid = new_id("clu")
    store.execute("INSERT INTO clusterings (id, split_id, computed_at, input_hash, n_trials, k, summary) "
                  "VALUES (?,?,?,?,?,?,?)", (cid, c.split_id, time.time(), c.input_hash, c.counts["trials"],
                                             json.dumps(c.k), json.dumps(c.counts)))
    for key, lab in c.labels.items():
        for exp_id, label in lab.items():
            store.execute("INSERT INTO cluster_members (clustering_id, threshold, experiment_id, label) "
                          "VALUES (?,?,?,?)", (cid, float(key), exp_id, label))
    for exp_id, row in c.flags.iterrows():
        store.execute("UPDATE return_index SET flat=?, passive=? WHERE experiment_id=?",
                      (int(row["flat"]), int(row["passive"]), exp_id))
    return cid


def latest(store: ExperimentStore, split_id: str) -> dict[str, Any] | None:
    ensure(store)
    rows = store.query("SELECT * FROM clusterings WHERE split_id=? ORDER BY computed_at DESC, rowid DESC LIMIT 1",
                       (split_id,))
    if not rows:
        return None
    row = rows[0]
    return row | {"k": json.loads(row["k"]), "summary": json.loads(row["summary"])}


def members(store: ExperimentStore, clustering_id: str, threshold: float) -> dict[str, str]:
    rows = store.query("SELECT experiment_id, label FROM cluster_members WHERE clustering_id=? AND threshold=? "
                       "ORDER BY experiment_id", (clustering_id, threshold))
    return {r["experiment_id"]: r["label"] for r in rows}


def effective_k(store: ExperimentStore, split_id: str) -> int | None:
    last = latest(store, split_id)
    return None if last is None else int(last["k"][_key(K_THRESHOLD)])
