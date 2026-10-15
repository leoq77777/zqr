"""Seed characterisation runs (appendix A7): each seed instance runs once on dev as a kind `seed` experiment.

They count as trials on the split and anchor the clustering; they do not select seeds.
"""

from __future__ import annotations

import json
from typing import Any

from quant_research.desk.config import load_config
from quant_research.experiments.gatekeeper import GateContext
from quant_research.experiments.runner import ExperimentRequest, run_experiment
from quant_research.experiments.store import ExperimentStore
from quant_research.library.catalog import Catalog, load_catalog
from quant_research.paths import Paths
from quant_research.research.mandate import Mandate
from quant_research.strategy.sandbox import SandboxConfig


def seed_runs(store: ExperimentStore, split_id: str) -> dict[str, str]:
    """instance id -> experiment id of its seed run on the split."""
    rows = store.query("SELECT id, json_extract(notes, '$.instance') AS instance FROM experiments "
                       "WHERE kind='seed' AND split_id=? AND status NOT IN ('blocked') ORDER BY created_at",
                       (split_id,))
    return {r["instance"]: r["id"] for r in rows if r["instance"]}


def characterise(paths: Paths, store: ExperimentStore, catalog: Catalog | None = None,
                 entries: set[str] | None = None, sandbox: SandboxConfig | None = None) -> list[dict[str, Any]]:
    """Run every seed instance not yet run on its product's desk mandate (dev segment, the mandate's costs)."""
    catalog = catalog or load_catalog()
    cfg = load_config(paths)
    out = []
    for product, mandate_id in sorted(cfg.mandates.items()):
        mandate = Mandate.model_validate(store.get_mandate(mandate_id)["body"])
        done = seed_runs(store, mandate.split_id)
        for inst in catalog.instances():
            if inst.product != product or (entries and inst.entry not in entries) or inst.id in done:
                continue
            req = ExperimentRequest(strategy_ref=inst.strategy_ref, parameters=inst.parameters,
                                    dataset_id=mandate.dataset_id, split_id=mandate.split_id, segment="dev",
                                    engine_config=mandate.engine_config, policy=mandate.policy)
            res = run_experiment(paths, store, req, kind="seed", sandbox=sandbox,
                                 gate=GateContext(caller="controller", reuse_duplicates=False),
                                 record={"notes": json.dumps({"library_entry": inst.entry, "instance": inst.id})})
            out.append({"instance": inst.id, "experiment_id": res.experiment_id, "status": res.status,
                        "sharpe": (res.metrics or {}).get("sharpe")})
    return out


def seed_periods(paths: Paths, store: ExperimentStore, product: str, cuts: list[int]) -> dict[str, Any]:
    """Period statistics of each seed run on the product's desk split (dev data only)."""
    import pandas as pd

    from quant_research.evaluation.periods import period_stats
    from quant_research.memory.returns import equity_file

    mandate_id = load_config(paths).mandates.get(product)
    if mandate_id is None:
        raise ValueError(f"no desk mandate for {product}")
    split_id = store.get_mandate(mandate_id)["body"]["split_id"]
    out: dict[str, Any] = {}
    for instance, exp_id in sorted(seed_runs(store, split_id).items()):
        row = store.get_experiment(exp_id)
        path = equity_file(paths, row) if row["status"] == "completed" else None
        out[instance] = ({"experiment_id": exp_id, "status": row["status"], "periods": None} if path is None else
                         {"experiment_id": exp_id, "status": row["status"],
                          "periods": period_stats(pd.read_parquet(path), row["engine_config"]["initial_cash"], cuts)})
    return {"product": product, "split_id": split_id, "seeds": out}


def seed_summary(paths: Paths, store: ExperimentStore, instance: str) -> dict[str, Any] | None:
    """The instance's latest seed run: dev Sharpe (daily) and split DSR at the split's latest K."""
    from quant_research.evaluation.split_stats import dsr_k, sharpe_daily
    from quant_research.memory.clusters import effective_k
    from quant_research.memory.returns import ReturnArchive

    rows = store.query("SELECT id, split_id, status FROM experiments WHERE kind='seed' "
                       "AND json_extract(notes, '$.instance')=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                       (instance,))
    if not rows:
        return None
    run = rows[0]
    returns = ReturnArchive(paths, store).load(run["split_id"])
    k = effective_k(store, run["split_id"])
    out = {"experiment_id": run["id"], "split_id": run["split_id"], "status": run["status"], "k": k,
           "dev_sharpe": None, "split_dsr": None}
    if run["id"] in returns.columns:
        r = returns[run["id"]].to_numpy()
        out["dev_sharpe"] = round(sharpe_daily(r), 4)
        out["split_dsr"] = None if k is None else round(dsr_k(r, k), 4)
    return out
