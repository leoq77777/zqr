"""Portfolio versions (design 5.1): immutable, hashed per product; only the lifecycle state changes.

draft -> validated | failed_validation (controller, deterministic standard), validated -> frozen (human),
frozen -> final_evaluated | failed_final (evaluator, the holdout gate), final_evaluated -> paper (human); any
non-terminal state -> retired (human). Every change is a `portfolio_events` row (appendix A14 point 3).
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quant_research.backtest.engine import EngineConfig
from quant_research.experiments.store import ExperimentStore
from quant_research.hashing import hash_obj

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_versions (
    id TEXT PRIMARY KEY, product TEXT NOT NULL, split_id TEXT NOT NULL, parent_id TEXT, state TEXT NOT NULL,
    body TEXT NOT NULL, evidence TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, version_id TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
    actor TEXT NOT NULL, evidence TEXT, note TEXT, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS validation_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT, split_id TEXT NOT NULL, subject TEXT NOT NULL, experiment_id TEXT,
    at REAL NOT NULL);
"""

Actor = Literal["controller", "human", "evaluator"]
TERMINAL = {"failed_validation", "failed_final", "retired"}
TRANSITIONS: dict[tuple[str, str], set[str]] = {
    ("draft", "validated"): {"controller"},
    ("draft", "failed_validation"): {"controller"},
    ("draft", "admitted"): {"controller"},  # passed gates 1 and 2; waits for the next split (A13.7)
    ("validated", "frozen"): {"human"},
    ("frozen", "final_evaluated"): {"evaluator"},
    ("frozen", "failed_final"): {"evaluator"},
    ("final_evaluated", "paper"): {"human"},
}


class VersionError(RuntimeError):
    pass


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_id: str
    family: str
    strategy_ref: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: Literal["member", "probation"] = "member"
    probation_start: str | None = None  # ISO date; phase 3b


class Overlay(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_id: str
    strategy_ref: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class Ladder(BaseModel):
    """Drawdown ladder on the portfolio's own equity (investment policy 3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    yellow: float = -0.20
    halve: float = -0.25
    liquidate: float = -0.30
    recover: float = -0.15
    halved_scale: float = 0.5

    @model_validator(mode="after")
    def _ordered(self):
        if not self.liquidate <= self.halve <= self.recover <= 0 or not 0 <= self.halved_scale <= 1:
            raise ValueError("ladder levels must satisfy liquidate <= halve <= recover <= 0")
        return self


NO_LADDER = Ladder(yellow=-1e9, halve=-1e9, liquidate=-1e9, recover=-1e9, halved_scale=1.0)


class PortfolioVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product: str
    granularity: str = "ONE_HOUR"
    dataset_id: str
    split_id: str
    members: tuple[Member, ...]
    overlay: Overlay | None = None
    member_cap: float = Field(0.15, gt=0, le=1)
    band: float = Field(0.10, ge=0, le=1)
    ladder: Ladder = Field(default_factory=Ladder)
    engine_config: EngineConfig
    parent_id: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _paper_reproducible(self):
        ec = self.engine_config
        if ec.execution_delay_bars or ec.adverse_fill or ec.rebalance_tolerance:
            raise ValueError("portfolio versions use no execution delay, no adverse fill and no rebalance "
                             "tolerance (the signal deadband replaces it)")
        if not self.members:
            raise ValueError("a portfolio version needs members")
        ids = [m.entry_id for m in self.members]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate member entry ids")
        return self

    def version_id(self) -> str:
        return "pv-" + hash_obj(self.model_dump(mode="json"))[:12]


class VersionStore:
    def __init__(self, store: ExperimentStore):
        self.store = store
        store.ensure_schema(SCHEMA)

    @staticmethod
    def _row(r: dict[str, Any]) -> dict[str, Any]:
        return r | {"body": json.loads(r["body"]), "evidence": json.loads(r["evidence"]) if r["evidence"] else None}

    def put(self, version: PortfolioVersion, actor: Actor = "controller") -> str:
        """Insert a draft; an existing id is returned unchanged (the body never changes)."""
        vid = version.version_id()
        if self.get(vid) is None:
            now = time.time()
            self.store.execute("INSERT OR IGNORE INTO portfolio_versions VALUES (?,?,?,?,?,?,?,?,?)",
                               (vid, version.product, version.split_id, version.parent_id, "draft",
                                json.dumps(version.model_dump(mode="json"), sort_keys=True), None, now, now))
            self._event(vid, None, "draft", actor, None, "defined", now)
        return vid

    def get(self, vid: str) -> dict[str, Any] | None:
        rows = self.store.query("SELECT * FROM portfolio_versions WHERE id=?", (vid,))
        return self._row(rows[0]) if rows else None

    def version(self, vid: str) -> PortfolioVersion:
        row = self.get(vid)
        if row is None:
            raise VersionError(f"unknown portfolio version {vid}")
        return PortfolioVersion.model_validate(row["body"])

    def list(self, product: str | None = None, state: str | None = None,
             split_id: str | None = None) -> list[dict[str, Any]]:
        where, args = ["1=1"], []
        for col, val in (("product", product), ("state", state), ("split_id", split_id)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        return [self._row(r) for r in self.store.query(
            f"SELECT * FROM portfolio_versions WHERE {' AND '.join(where)} ORDER BY created_at, id", args)]

    def transition(self, vid: str, to: str, actor: Actor, evidence: dict[str, Any] | None = None,
                   note: str = "") -> dict[str, Any]:
        row = self.get(vid)
        if row is None:
            raise VersionError(f"unknown portfolio version {vid}")
        cur = row["state"]
        if to == "retired":
            if cur in TERMINAL:
                raise VersionError(f"version is already {cur}")
            if actor != "human":
                raise VersionError("only a human can retire a portfolio version")
        elif actor not in TRANSITIONS.get((cur, to), set()):
            allowed = TRANSITIONS.get((cur, to))
            raise VersionError(f"{cur} -> {to} is not allowed for {actor}" if allowed
                               else f"illegal transition {cur} -> {to}")
        now = time.time()
        merged = {**(row["evidence"] or {}), **(evidence or {})}
        self.store.execute("UPDATE portfolio_versions SET state=?, evidence=?, updated_at=? WHERE id=?",
                           (to, json.dumps(merged, sort_keys=True, default=str), now, vid))
        self._event(vid, cur, to, actor, evidence, note, now)
        return self.get(vid)

    def _event(self, vid: str, frm: str | None, to: str, actor: str, evidence: dict | None, note: str,
               at: float) -> None:
        self.store.execute("INSERT INTO portfolio_events (version_id, from_state, to_state, actor, evidence, note, "
                           "at) VALUES (?,?,?,?,?,?,?)",
                           (vid, frm, to, actor, json.dumps(evidence, default=str) if evidence else None, note, at))

    def record(self, vid: str, actor: Actor, evidence: dict[str, Any], note: str = "") -> None:
        """An event that leaves the state unchanged (for example the start of a shadow trader)."""
        row = self.get(vid)
        if row is None:
            raise VersionError(f"unknown portfolio version {vid}")
        self._event(vid, row["state"], row["state"], actor, evidence, note, time.time())

    def events(self, vid: str) -> list[dict[str, Any]]:
        return self.store.query("SELECT * FROM portfolio_events WHERE version_id=? ORDER BY id", (vid,))

    def record_validation_use(self, split_id: str, subject: str, experiment_id: str | None) -> None:
        self.store.execute("INSERT INTO validation_uses (split_id, subject, experiment_id, at) VALUES (?,?,?,?)",
                           (split_id, subject, experiment_id, time.time()))

    def validation_uses(self, split_id: str) -> int:
        return self.store.query("SELECT COUNT(*) AS n FROM validation_uses WHERE split_id=?", (split_id,))[0]["n"]
