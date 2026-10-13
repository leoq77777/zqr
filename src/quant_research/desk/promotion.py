"""Automatic promotion under the standing authorization: validated -> frozen -> holdout -> paper_approved -> roster.

Every step stops at the first refusal and says why; the candidate keeps whatever state it reached.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from quant_research.backtest.engine import EngineConfig
from quant_research.data.snapshot import load_manifest
from quant_research.experiments.store import ExperimentStore
from quant_research.paper.fleet import RosterEntry, add_entry, load_roster
from quant_research.paths import Paths
from quant_research.research import authorization as auth_mod
from quant_research.research.authorization import AuthorizationError
from quant_research.research.mandate import Mandate
from quant_research.research.registry import RegistryError, freeze, transition
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.validation.holdout import HoldoutError, run_candidate_holdout

log = logging.getLogger(__name__)
PAPER_STATES = ("paper_approved", "paper_active")
# Engine settings the paper trader cannot reproduce; a candidate validated with them is not promoted.
PAPER_UNSUPPORTED = ("execution_delay_bars", "adverse_fill", "rebalance_tolerance")
RESUMABLE = ("validated", "frozen", "final_evaluated", "paper_approved")


def paper_gaps(engine: EngineConfig) -> list[str]:
    default = EngineConfig()
    return [f"{k}={getattr(engine, k)!r}" for k in PAPER_UNSUPPORTED if getattr(engine, k) != getattr(default, k)]


def authorization_path(paths: Paths):
    return paths.desk / "authorization.json"


def roster_path(paths: Paths):
    return paths.desk / "roster.json"


def _paper_candidates(paths: Paths) -> int:
    path = roster_path(paths)
    if not path.exists():
        return 0
    return sum(1 for e in load_roster(path).entries if e.enabled and e.source == "candidate")


def promote(paths: Paths, store: ExperimentStore, candidate_id: str, sandbox: SandboxConfig | None = None,
            now: float | None = None) -> dict[str, Any]:
    """Push a candidate as far as the authorization and the mandate allow, resuming from the state it reached.

    Paper slots are checked before the holdout so a full roster never spends a viewing; a frozen candidate whose
    holdout already ran gets that result back (see `run_holdout`); a paper_approved candidate only needs its
    roster entry.
    """
    out: dict[str, Any] = {"candidate_id": candidate_id, "steps": []}

    def stop(reason: str) -> dict[str, Any]:
        cand = store.get_candidate(candidate_id)
        return out | {"state": cand["state"] if cand else None, "stopped": reason}

    def state() -> str:
        return store.get_candidate(candidate_id)["state"]

    cand = store.get_candidate(candidate_id)
    if cand is None or cand["state"] not in RESUMABLE:
        return stop(f"nothing to promote (state {cand['state'] if cand else 'unknown'})")
    auth = auth_mod.load(authorization_path(paths))
    if auth is None:
        return stop("no standing authorization; waiting for a human (`zqr candidate freeze`)")
    mandate = Mandate.model_validate(store.get_mandate(cand["mandate_id"])["body"])
    manifest = load_manifest(paths, mandate.dataset_id)
    try:
        auth.check(now=now, product=manifest["instrument"])
    except AuthorizationError as exc:
        return stop(str(exc))
    gaps = paper_gaps(mandate.engine_config)
    if gaps:
        return stop("paper cannot run the validated configuration (" + ", ".join(gaps)
                    + "); revalidate without these settings")
    full = f"paper roster already has {auth.max_paper_strategies} candidate strategies (authorization cap)"

    try:
        if state() != "paper_approved" and _paper_candidates(paths) >= auth.max_paper_strategies:
            return stop(full)
        if state() == "validated":
            freeze(paths, store, candidate_id, "desk standing authorization", actor="desk", authorization=auth)
            out["steps"].append("frozen")
        if state() == "frozen":
            res = run_candidate_holdout(paths, store, candidate_id, sandbox)
            out["steps"].append("final_evaluated" if res["passed"] else "failed_final")
            out["holdout"] = {k: v for k, v in res["candidate"]["evidence"].items() if k != "rule"}
            if not res["passed"]:
                return stop("holdout pass rule not met")
        if state() == "final_evaluated":
            if _paper_candidates(paths) >= auth.max_paper_strategies:
                return stop(full)
            transition(store, candidate_id, "paper_approved", "desk", note="desk standing authorization",
                       authorization=auth)
            out["steps"].append("paper_approved")
    except (RegistryError, HoldoutError) as exc:
        return stop(str(exc))

    engine = mandate.engine_config
    entry = RosterEntry(name=f"{manifest['instrument'].split('-')[0].lower()}-{candidate_id}",
                        product=manifest["instrument"], granularity=manifest["granularity"],
                        strategy_ref=cand["strategy_ref"], parameters=cand["parameters"], policy=mandate.policy,
                        initial_cash=engine.initial_cash, fee_rate=engine.costs.fee_rate,
                        slippage_bps=engine.costs.slippage_bps, min_trade_notional=engine.min_trade_notional,
                        source="candidate", candidate_id=candidate_id)
    if add_entry(roster_path(paths), entry):
        out["steps"].append("roster")
    out["roster_name"] = entry.name
    return stop("done")


def activate_hook(paths: Paths) -> Callable[[RosterEntry], None]:
    """Fleet callback: a candidate entry that starts trading moves paper_approved -> paper_active (actor paper)."""

    def _activate(entry: RosterEntry) -> None:
        if entry.source != "candidate" or not entry.candidate_id:
            return
        store = ExperimentStore(paths.db)
        try:
            cand = store.get_candidate(entry.candidate_id)
            if cand and cand["state"] == "paper_approved":
                transition(store, entry.candidate_id, "paper_active", "paper",
                           {**(cand["evidence"] or {}), "paper_name": entry.name}, "attached to the paper fleet")
        finally:
            store.close()

    return _activate
