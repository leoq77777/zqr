"""The human's steps between the holdout and the paper fleet: approve a version (roster entry, library statuses)
and resume a liquidated portfolio (investment policy 3)."""

from __future__ import annotations

import json
import time
from typing import Any

from quant_research.desk.promotion import roster_path
from quant_research.experiments.store import ExperimentStore
from quant_research.library.store import LibraryStore
from quant_research.paper.fleet import RosterEntry, add_entry, load_roster
from quant_research.paper.ledger import Ledger
from quant_research.paper.portfolio_trader import RESUME_FILE, STATE_KEY
from quant_research.paths import Paths
from quant_research.portfolio.blend import has_plugin
from quant_research.portfolio.version import VersionError, VersionStore

SHADOW_STATES = {"validated", "failed_validation", "failed_final"}


def entry_name(product: str) -> str:
    return f"portfolio-{product.lower()}"


def shadow_name(product: str) -> str:
    return f"shadow-{product.lower()}"


def _entry(version, vid: str, name: str, shadow: bool) -> RosterEntry:
    eng = version.engine_config
    return RosterEntry(name=name, product=version.product, granularity=version.granularity,
                       strategy_ref=f"portfolio:{vid}", parameters={"version_id": vid},
                       initial_cash=eng.initial_cash, fee_rate=eng.costs.fee_rate,
                       slippage_bps=eng.costs.slippage_bps, min_trade_notional=eng.min_trade_notional,
                       source="portfolio", portfolio_version=vid, shadow=shadow)


def _paper_ready(vs: VersionStore, vid: str):
    version = vs.version(vid)
    if has_plugin(version):
        raise VersionError(f"{vid} has a plugin member or overlay; paper runs template strategies only (plugins run "
                           "in the docker sandbox in backtests)")
    return version


def start_shadow(paths: Paths, store: ExperimentStore, vid: str, note: str) -> dict[str, Any]:
    """Run a version that is not approved for paper as the shadow trader `shadow-<product>` (phase 4, D2): forward
    evidence only. The version's state and the library statuses do not change; the start is a version event."""
    if not note.strip():
        raise VersionError("a shadow start needs a note")
    vs = VersionStore(store)
    row = vs.get(vid)
    if row is None:
        raise VersionError(f"unknown portfolio version {vid}")
    if row["state"] not in SHADOW_STATES:
        raise VersionError(f"a shadow runs a version in {sorted(SHADOW_STATES)} ({vid} is {row['state']})")
    version = _paper_ready(vs, vid)
    name = shadow_name(version.product)
    path = roster_path(paths)
    if path.exists() and any(e.name == name for e in load_roster(path).entries):
        raise VersionError(f"the roster already has {name}")
    add_entry(path, _entry(version, vid, name, shadow=True))
    vs.record(vid, "human", {"shadow": name}, note)
    return {"version_id": vid, "state": row["state"], "roster_name": name, "shadow": True}


def approve(paths: Paths, store: ExperimentStore, vid: str, note: str) -> dict[str, Any]:
    """final_evaluated -> paper: adds the roster entry `portfolio-<product>` and moves the version's candidate
    library entries to member."""
    vs = VersionStore(store)
    row = vs.get(vid)
    if row is None:
        raise VersionError(f"unknown portfolio version {vid}")
    if row["state"] != "final_evaluated":
        raise VersionError(f"only a version that passed the holdout is approved for paper ({vid} is {row['state']})")
    version = _paper_ready(vs, vid)
    name = entry_name(version.product)
    path = roster_path(paths)
    if path.exists() and any(e.name == name for e in load_roster(path).entries):
        raise VersionError(f"the roster already has {name}; replacing a running portfolio is phase 3b")
    entry = _entry(version, vid, name, shadow=False)
    lib = LibraryStore(paths.db)
    moved = []
    try:
        ids = [m.entry_id for m in version.members] + ([version.overlay.entry_id] if version.overlay else [])
        for eid in ids:
            e = lib.entry(eid)
            if e is not None and e["status"] == "candidate":
                lib.set_status(eid, "member", "human", f"in portfolio {vid} approved for paper: {note}")
                moved.append(eid)
    finally:
        lib.close()
    add_entry(path, entry)
    vs.transition(vid, "paper", "human", {"roster_name": name}, note)
    return {"version_id": vid, "state": "paper", "roster_name": name, "library_members": moved}


def request_resume(paths: Paths, name: str, note: str) -> dict[str, Any]:
    """Ask the running portfolio trader `name` to resume after a liquidation (applied at its next bar)."""
    if not note.strip():
        raise VersionError("a resume needs a note")
    workdir = paths.paper / name
    if not (workdir / "paper.db").exists():
        raise VersionError(f"no paper ledger for {name}")
    ledger = Ledger(workdir / "paper.db")
    try:
        state = ledger.get(STATE_KEY)
    finally:
        ledger.close()
    if not state or state.get("level") != "liquidated":
        raise VersionError(f"{name} is not liquidated (level {(state or {}).get('level')}); nothing to resume")
    request = {"note": note, "at": time.time()}
    (workdir / RESUME_FILE).write_text(json.dumps(request))
    return {"name": name, "requested": request, "state": state}
