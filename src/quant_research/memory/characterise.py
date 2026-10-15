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
