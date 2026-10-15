"""The human's steps between the holdout and the paper fleet: approve a version (roster entry, library statuses)
and resume a liquidated portfolio (investment policy 3)."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from quant_research.desk.promotion import roster_path
from quant_research.experiments.store import ExperimentStore
from quant_research.library.store import LibraryStore
from quant_research.paper.fleet import RosterEntry, add_entry, load_roster
from quant_research.paper.ledger import Ledger
from quant_research.paper.portfolio_trader import LIQUIDATED_KEY, RESUME_FILE, RESUME_WAIT_SECONDS, STATE_KEY
from quant_research.paths import Paths
from quant_research.portfolio.blend import has_plugin
from quant_research.portfolio.version import VersionError, VersionStore

SHADOW_STATES = {"validated", "failed_validation", "failed_final"}
MIN_REVIEW_CHARS = 200


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


def request_resume(paths: Paths, name: str, note: str, review_ref: str, review: str,
                   now: float | None = None) -> dict[str, Any]:
    """Ask the running portfolio trader `name` to resume after a liquidation (applied at its next bar).

    Investment policy 3 (P6): no earlier than 30 days after the liquidation and only with a written review in
    `docs/reviews/`. The review's text travels with the request (production hosts have no repository), and the
    trader keeps it with the applied request."""
    if not note.strip():
        raise VersionError("a resume needs a note")
    ref = PurePosixPath(review_ref)
    if ref.parts[:2] != ("docs", "reviews") or ref.suffix != ".md" or ".." in ref.parts:
        raise VersionError("the review must be a docs/reviews/*.md file")
    if len(review.strip()) < MIN_REVIEW_CHARS:
        raise VersionError(f"the review is shorter than {MIN_REVIEW_CHARS} characters; it must cover the cause of the "
                           "drawdown, whether the thesis still holds and whether dev covered the regime")
    workdir = paths.paper / name
    if not (workdir / "paper.db").exists():
        raise VersionError(f"no paper ledger for {name}")
    ledger = Ledger(workdir / "paper.db")
    try:
        state, liquidated_at = ledger.get(STATE_KEY), ledger.get(LIQUIDATED_KEY)
    finally:
        ledger.close()
    if not state or state.get("level") != "liquidated":
        raise VersionError(f"{name} is not liquidated (level {(state or {}).get('level')}); nothing to resume")
    now = time.time() if now is None else now
    if liquidated_at is None:
        raise VersionError(f"{name} has no liquidation time yet; it is recorded at the trader's next bar")
    if now - liquidated_at < RESUME_WAIT_SECONDS:
        earliest = datetime.fromtimestamp(liquidated_at + RESUME_WAIT_SECONDS, tz=UTC)
        raise VersionError(f"{name} was liquidated less than 30 days ago; resume from {earliest:%Y-%m-%d %H:%M} UTC")
    request = {"note": note, "at": now, "liquidated_at": liquidated_at, "review_ref": str(ref),
               "review_sha256": hashlib.sha256(review.encode()).hexdigest(), "review": review}
    (workdir / RESUME_FILE).write_text(json.dumps(request))
    return {"name": name, "requested": {k: v for k, v in request.items() if k != "review"}, "state": state}
