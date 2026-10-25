"""The human's steps between the holdout and the paper fleet: approve a version (roster entry, library statuses)
and resume a liquidated portfolio (investment policy 3)."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from quant_research.desk.promotion import authorization_path, roster_path
from quant_research.experiments.store import ExperimentStore
from quant_research.library.store import LibraryStore
from quant_research.paper.fleet import Roster, RosterEntry, add_entry, load_roster, save_roster
from quant_research.paper.ledger import Ledger
from quant_research.paper.portfolio_trader import LIQUIDATED_KEY, RESUME_FILE, RESUME_WAIT_SECONDS, STATE_KEY
from quant_research.paths import Paths
from quant_research.portfolio.blend import has_plugin
from quant_research.portfolio.version import Actor, VersionError, VersionStore
from quant_research.research.authorization import Authorization
from quant_research.research.authorization import load as load_authorization

SHADOW_STATES = {"validated", "failed_validation", "failed_final", "admitted"}
MIN_REVIEW_CHARS = 200


def entry_name(product: str) -> str:
    return f"portfolio-{product.lower()}"


def shadow_name(product: str) -> str:
    return f"shadow-{product.lower()}"


def admitted_shadow_name(product: str, vid: str) -> str:
    """A unique roster name so admitted shadows sit next to the seed `shadow-<product>` (A13.10)."""
    tail = vid.split("-", 1)[-1][:8]
    return f"shadow-{product.lower()}-admitted-{tail}"


def admitted_paper_name(product: str, vid: str) -> str:
    """Official paper slot for an admitted version under super authorization (A13.12)."""
    tail = vid.split("-", 1)[-1][:8]
    return f"paper-{product.lower()}-admitted-{tail}"


def _admitted_paper_count(roster: Roster) -> int:
    return sum(1 for e in roster.entries if e.enabled and not e.shadow and e.source == "portfolio"
               and e.portfolio_version and "admitted" in e.name)


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


def start_shadow(paths: Paths, store: ExperimentStore, vid: str, note: str,
                 actor: Actor = "human") -> dict[str, Any]:
    """Run a version that is not approved for paper as a shadow trader (phase 4 D2, A13.10): forward evidence
    only. Seed / failed versions use `shadow-<product>`; admitted versions use a unique
    `shadow-<product>-admitted-<vid>`. The version's state and the library statuses do not change."""
    if actor == "human" and not note.strip():
        raise VersionError("a shadow start needs a note")
    vs = VersionStore(store)
    row = vs.get(vid)
    if row is None:
        raise VersionError(f"unknown portfolio version {vid}")
    if row["state"] not in SHADOW_STATES:
        raise VersionError(f"a shadow runs a version in {sorted(SHADOW_STATES)} ({vid} is {row['state']})")
    version = _paper_ready(vs, vid)
    name = (admitted_shadow_name(version.product, vid) if row["state"] == "admitted"
            else shadow_name(version.product))
    path = roster_path(paths)
    if path.exists() and any(e.name == name for e in load_roster(path).entries):
        raise VersionError(f"the roster already has {name}")
    add_entry(path, _entry(version, vid, name, shadow=True))
    vs.record(vid, actor, {"shadow": name}, note.strip() or "admitted auto-shadow")
    return {"version_id": vid, "state": row["state"], "roster_name": name, "shadow": True}


def start_admitted_paper(paths: Paths, store: ExperimentStore, vid: str, note: str = "",
                         actor: Actor = "controller",
                         authorization: Authorization | None = None) -> dict[str, Any]:
    """Gate 1+2 admitted version onto the paper roster now (A13.12). No holdout, no next split, no per-version
    human. Version state stays `admitted`. An existing admitted shadow for this version is upgraded (same
    trader, now in the combined account)."""
    auth = authorization if authorization is not None else load_authorization(authorization_path(paths))
    if auth is None or not auth.auto_paper_admitted:
        raise VersionError("admitted paper needs a super authorization (`zqr desk authorize --renew --super`)")
    vs = VersionStore(store)
    row = vs.get(vid)
    if row is None:
        raise VersionError(f"unknown portfolio version {vid}")
    if row["state"] != "admitted":
        raise VersionError(f"only an admitted version goes to paper under super authorization ({vid} is "
                           f"{row['state']})")
    version = _paper_ready(vs, vid)
    auth.check(product=version.product)
    path = roster_path(paths)
    roster = load_roster(path) if path.exists() else Roster()
    for i, e in enumerate(roster.entries):
        if e.portfolio_version == vid and e.source == "portfolio":
            if not e.shadow:
                return {"version_id": vid, "state": "admitted", "roster_name": e.name, "shadow": False,
                        "already": True}
            roster.entries[i] = e.model_copy(update={"shadow": False})
            save_roster(path, roster)
            vs.record(vid, actor, {"paper": e.name, "upgraded_from_shadow": True},
                      note.strip() or "admitted auto-paper")
            return {"version_id": vid, "state": "admitted", "roster_name": e.name, "shadow": False,
                    "upgraded": True}
    if _admitted_paper_count(roster) >= auth.max_paper_strategies:
        raise VersionError(f"paper roster already has {auth.max_paper_strategies} admitted strategies "
                           "(authorization cap)")
    name = admitted_paper_name(version.product, vid)
    add_entry(path, _entry(version, vid, name, shadow=False))
    vs.record(vid, actor, {"paper": name}, note.strip() or "admitted auto-paper")
    return {"version_id": vid, "state": "admitted", "roster_name": name, "shadow": False}


def ensure_admitted_paper(paths: Paths, store: ExperimentStore) -> list[dict[str, Any]]:
    """Start paper for every admitted version the super authorization covers (including ones admitted earlier)."""
    from quant_research.hypothesis.store import HypothesisStore

    auth = load_authorization(authorization_path(paths))
    if auth is None or not auth.auto_paper_admitted:
        return []
    hs = HypothesisStore(store)
    out = []
    for row in hs.list(state="admitted"):
        vid = ((hs.event(row["id"], "gate2") or {}).get("evidence") or {}).get("version_id")
        if not vid:
            continue
        try:
            out.append(start_admitted_paper(paths, store, vid, "admitted auto-paper", authorization=auth))
        except VersionError as exc:
            out.append({"version_id": vid, "hypothesis_id": row["id"], "error": str(exc)})
    return out


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
