"""The seed portfolio of a product (design 5.8): every reviewed seed member of a sleeve family plus the overlay,
equal sleeves, fixed before validation or holdout is looked at. Costs, capital, dataset and split come from the
product's desk mandate."""

from __future__ import annotations

from quant_research.desk.config import load_config
from quant_research.experiments.store import ExperimentStore
from quant_research.library.store import LibraryStore
from quant_research.paths import Paths
from quant_research.portfolio.version import Member, Overlay, PortfolioVersion, VersionError, VersionStore
from quant_research.research.mandate import Mandate

OVERLAY_FAMILY = "vol_overlay"


def seed_version(paths: Paths, store: ExperimentStore, product: str) -> PortfolioVersion:
    mandate_id = load_config(paths).mandates.get(product)
    if mandate_id is None:
        raise VersionError(f"no desk mandate for {product}")
    rec = store.get_mandate(mandate_id)
    if rec is None:
        raise VersionError(f"desk mandate {mandate_id} of {product} is not stored")
    mandate = Mandate.model_validate(rec["body"])
    lib = LibraryStore(paths.db)
    try:
        sleeves = {f["id"] for f in lib.families() if f["sleeve"]}
        entries = [e for e in lib.entries(product=product) if e["status"] != "retired" and e["source"] == "seed"]
    finally:
        lib.close()
    members = tuple(Member(entry_id=e["id"], family=e["family"], strategy_ref=e["strategy_ref"],
                           parameters=e["parameters"])
                    for e in entries if e["family"] in sleeves and e["kind"] == "member")
    overlays = [e for e in entries if e["family"] == OVERLAY_FAMILY and e["kind"] == "overlay"]
    if not members:
        raise VersionError(f"no library members for {product}; run `zqr library import`")
    if len(overlays) > 1:
        raise VersionError(f"{product} has {len(overlays)} overlay entries; the seed portfolio takes one")
    overlay = (Overlay(entry_id=overlays[0]["id"], strategy_ref=overlays[0]["strategy_ref"],
                       parameters=overlays[0]["parameters"]) if overlays else None)
    return PortfolioVersion(
        product=product, dataset_id=mandate.dataset_id, split_id=mandate.split_id, members=members, overlay=overlay,
        engine_config=mandate.engine_config.model_copy(update={"rebalance_tolerance": 0.0}),
        notes="seed portfolio (design 5.8)")


def define_seed(paths: Paths, store: ExperimentStore, product: str) -> str:
    return VersionStore(store).put(seed_version(paths, store, product))
