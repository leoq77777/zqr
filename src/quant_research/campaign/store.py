"""Campaigns in the experiment database: the record, its state history, its sessions and the family quotas (P1).

States (design 6.1, P5): `proposed` -> `queued` (the human approves) -> `running` -> `concluded` | `exhausted`
(a cap reached first, P3); `cancelled` by the human from any state before the end. One campaign runs at a time.
The body never changes; caps grow only by the top-ups of D7, recorded as events.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from quant_research.campaign.model import (
    CAPS_MAX,
    CAPS_START,
    EXPLORATION,
    TERMINAL,
    TOPUP,
    TOPUP_REASONS,
    CampaignSpec,
)
from quant_research.desk.config import load_config
from quant_research.experiments.store import ExperimentStore
from quant_research.hashing import hash_obj
from quant_research.hypothesis.store import HypothesisError, HypothesisStore, check_citations
from quant_research.library.store import LibraryStore
from quant_research.paths import Paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY, product TEXT NOT NULL, split_id TEXT NOT NULL, family TEXT NOT NULL, body TEXT NOT NULL,
    source TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS campaign_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL, event TEXT NOT NULL, actor TEXT NOT NULL,
    data TEXT, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS campaign_sessions (
    session_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, run_id TEXT NOT NULL, allowance INTEGER NOT NULL,
    reserved_usd REAL NOT NULL, last INTEGER NOT NULL, cost_usd REAL, experiments INTEGER, notes TEXT,
    failure_class TEXT, started_at REAL NOT NULL, finished_at REAL);
"""
# sessions that failed for reasons outside the agent's work do not use up a campaign's session cap
UNCOUNTED = ("environment.model_unreachable", "environment.docker", "harness.error", "harness.crash",
             "desk.stopped")

FAMILY_QUOTA = 100  # trials per family per product split (P1)
EXPLORATION_SHARE = 0.25  # of the split's family quotas (P1)
TRANSITIONS: dict[tuple[str, str], set[str]] = {
    ("proposed", "queued"): {"human"},
    ("proposed", "cancelled"): {"human"},
    ("queued", "cancelled"): {"human"},
    ("queued", "running"): {"controller"},
    ("running", "cancelled"): {"human"},
    ("running", "concluded"): {"controller"},
    ("running", "exhausted"): {"controller"},
}
TRIAL_SQL = "e.segment='dev' AND e.status != 'blocked' AND e.kind != 'sensitivity'"


class CampaignError(ValueError):
    pass


def sleeve_families(paths: Paths) -> list[str]:
    lib = LibraryStore(paths.db)
    try:
        return [f["id"] for f in lib.families() if f["sleeve"]]
    finally:
        lib.close()


def desk_split(paths: Paths, store: ExperimentStore, product: str) -> str:
    mandate_id = load_config(paths).mandates.get(product)
    if mandate_id is None:
        raise CampaignError(f"no desk mandate for {product}")
    return store.get_mandate(mandate_id)["body"]["split_id"]


class CampaignStore:
    def __init__(self, store: ExperimentStore):
        self.store = store
        store.ensure_schema(SCHEMA)
        HypothesisStore(store)

    # -- record ------------------------------------------------------------------------------------
    def add(self, paths: Paths, spec: CampaignSpec, source: dict[str, Any], actor: str) -> dict[str, Any]:
        """A new campaign in `proposed`; refused for a family at quota or a model without a price (P1, P2)."""
        split_id = desk_split(paths, self.store, spec.product)
        sleeves = sleeve_families(paths)
        if spec.family != EXPLORATION and spec.family not in sleeves:
            raise CampaignError(f"family {spec.family!r} is not a sleeve family ({', '.join(sleeves)}) "
                                f"or {EXPLORATION!r}")
        if spec.model is not None and spec.model not in load_config(paths).campaign_models:
            raise CampaignError(f"model {spec.model!r} has no price in desk config `campaign_models`")
        try:
            check_citations(self.store, spec.citations, split_id)
        except HypothesisError as exc:
            raise CampaignError(str(exc)) from None
        q = self.quota(paths, split_id, spec.family)
        if q["room"] <= 0:
            raise CampaignError(f"{spec.family} is at its quota on {split_id} ({q['used']} of {q['quota']} trials)")
        body = spec.model_dump(mode="json")
        now = time.time()
        cid = "cmp-" + hash_obj({"body": body, "split_id": split_id, "source": source, "at": now})[:12]
        self.store.execute("INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?,?)",
                           (cid, spec.product, split_id, spec.family, json.dumps(body, sort_keys=True),
                            json.dumps(source, sort_keys=True, default=str), "proposed", now, now))
        self.record(cid, "proposed", actor, {"source": source})
        return self.get(cid)

    def get(self, cid: str) -> dict[str, Any] | None:
        rows = self.store.query("SELECT * FROM campaigns WHERE id=?", (cid,))
        return self._row(rows[0]) if rows else None

    def require(self, cid: str) -> dict[str, Any]:
        row = self.get(cid)
        if row is None:
            raise CampaignError(f"unknown campaign {cid}")
        return row

    def spec(self, cid: str) -> CampaignSpec:
        return CampaignSpec.model_validate(self.require(cid)["body"])

    def list(self, product: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        where, args = ["1=1"], []
        for col, val in (("product", product), ("state", state)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        return [self._row(r) for r in self.store.query(
            f"SELECT * FROM campaigns WHERE {' AND '.join(where)} ORDER BY created_at, id", args)]

    @staticmethod
    def _row(r: dict[str, Any]) -> dict[str, Any]:
        return r | {"body": json.loads(r["body"]), "source": json.loads(r["source"])}

    # -- state -------------------------------------------------------------------------------------
    def transition(self, cid: str, to: str, actor: str, note: str = "", data: dict[str, Any] | None = None) -> None:
        row = self.require(cid)
        allowed = TRANSITIONS.get((row["state"], to))
        if allowed is None:
            raise CampaignError(f"{cid} is {row['state']}; it cannot become {to}")
        if actor not in allowed:
            raise CampaignError(f"{row['state']} -> {to} is for the {' or '.join(sorted(allowed))}, not {actor}")
        if actor == "human" and not note.strip():
            raise CampaignError("the human's decision needs a note")
        if to == "running" and (other := self.running()) is not None:
            raise CampaignError(f"{other['id']} is running; one campaign runs at a time")
        self.store.execute("UPDATE campaigns SET state=?, updated_at=? WHERE id=?", (to, time.time(), cid))
        self.record(cid, to, actor, (data or {}) | ({"note": note} if note else {}))

    def record(self, cid: str, event: str, actor: str, data: dict[str, Any] | None = None) -> None:
        self.store.execute("INSERT INTO campaign_events (campaign_id, event, actor, data, at) VALUES (?,?,?,?,?)",
                           (cid, event, actor, json.dumps(data or {}, sort_keys=True, default=str), time.time()))

    def events(self, cid: str) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM campaign_events WHERE campaign_id=? ORDER BY id", (cid,))
        return [r | {"data": json.loads(r["data"] or "{}")} for r in rows]

    def event(self, cid: str, name: str) -> dict[str, Any] | None:
        found = [e for e in self.events(cid) if e["event"] == name]
        return found[-1] if found else None

    def running(self) -> dict[str, Any] | None:
        rows = self.list(state="running")
        return rows[0] if rows else None

    def next_queued(self) -> dict[str, Any] | None:
        """The campaign approved first."""
        rows = self.store.query("SELECT c.id FROM campaigns c JOIN campaign_events e ON e.campaign_id = c.id AND "
                                "e.event='queued' WHERE c.state='queued' ORDER BY e.id LIMIT 1")
        return self.get(rows[0]["id"]) if rows else None

    # -- caps and usage (P3) -----------------------------------------------------------------------
    def topup(self, cid: str, reason: str) -> bool:
        """+$2, 2 sessions, 20 experiments after a gate pass (D7); once per reason."""
        if reason not in TOPUP_REASONS:
            raise CampaignError(f"unknown top-up reason {reason!r}")
        if any(e["data"].get("reason") == reason for e in self.events(cid) if e["event"] == "topup"):
            return False
        self.record(cid, "topup", "controller", {"reason": reason, **TOPUP})
        return True

    def caps(self, cid: str) -> dict[str, float]:
        n = sum(e["event"] == "topup" for e in self.events(cid))
        return {k: min(CAPS_START[k] + n * TOPUP[k], CAPS_MAX[k]) for k in CAPS_START}

    def sessions(self, cid: str) -> list[dict[str, Any]]:
        return self.store.query("SELECT * FROM campaign_sessions WHERE campaign_id=? ORDER BY started_at, rowid",
                                (cid,))

    def campaign_of_session(self, sid: str) -> str | None:
        rows = self.store.query("SELECT campaign_id FROM campaign_sessions WHERE session_id=?", (sid,))
        return rows[0]["campaign_id"] if rows else None

    def session(self, sid: str) -> dict[str, Any] | None:
        rows = self.store.query("SELECT * FROM campaign_sessions WHERE session_id=?", (sid,))
        return rows[0] if rows else None

    def start_session(self, cid: str, sid: str, run_id: str, allowance: int, reserved_usd: float,
                      last: bool) -> None:
        self.store.execute("INSERT INTO campaign_sessions (session_id, campaign_id, run_id, allowance, reserved_usd, "
                           "last, started_at) VALUES (?,?,?,?,?,?,?)",
                           (sid, cid, run_id, allowance, reserved_usd, int(last), time.time()))

    def finish_session(self, sid: str, cost_usd: float, experiments: int, notes: str | None,
                       failure_class: str) -> None:
        self.store.execute("UPDATE campaign_sessions SET cost_usd=?, experiments=?, notes=?, failure_class=?, "
                           "finished_at=? WHERE session_id=?",
                           (cost_usd, experiments, notes, failure_class, time.time(), sid))

    def marginal_evals(self, cid: str) -> list[str]:
        rows = self.store.query(
            "SELECT DISTINCT json_extract(v.data, '$.experiment_id') AS e FROM session_events v JOIN "
            "campaign_sessions s ON s.session_id = v.session_id WHERE s.campaign_id=? AND v.event='marginal_eval' "
            "ORDER BY e", (cid,))
        return [r["e"] for r in rows if r["e"]]

    def session_trials(self, sid: str) -> int:
        return self.store.query(f"SELECT COUNT(*) AS n FROM experiments e WHERE e.session_id=? AND {TRIAL_SQL}",
                                (sid,))[0]["n"]

    def usage(self, cid: str) -> dict[str, float]:
        """Booked dollars (a running session counts at its reservation), sessions and trials (dev experiments of
        its sessions plus distinct marginal evaluations)."""
        rows = self.sessions(cid)
        usd = sum(r["cost_usd"] if r["finished_at"] is not None else r["reserved_usd"] for r in rows)
        trials = sum(self.session_trials(r["session_id"]) for r in rows) + len(self.marginal_evals(cid))
        counted = sum(r["failure_class"] not in UNCOUNTED for r in rows)
        return {"usd": round(usd, 6), "sessions": counted, "experiments": trials}

    def remaining(self, cid: str) -> dict[str, float]:
        caps, used = self.caps(cid), self.usage(cid)
        return {k: round(caps[k] - used[k], 6) for k in caps}

    # -- quotas (P1) -------------------------------------------------------------------------------
    def family_trials(self, split_id: str, family: str) -> int:
        """Trials of a family on the split and its ancestors (A13.8): dev experiments of their campaigns' sessions,
        their marginal evaluations and the variant runs of their registered hypotheses."""
        return sum(self._own_family_trials(s, family) for s in [split_id, *self.store.split_ancestors(split_id)])

    def _own_family_trials(self, split_id: str, family: str) -> int:
        sessions = self.store.query(
            f"SELECT COUNT(*) AS n FROM experiments e JOIN campaign_sessions s ON s.session_id = e.session_id "
            f"JOIN campaigns c ON c.id = s.campaign_id WHERE c.split_id=? AND c.family=? AND e.split_id=? "
            f"AND {TRIAL_SQL}", (split_id, family, split_id))[0]["n"]
        evals = sum(len(self.marginal_evals(r["id"])) for r in self.store.query(
            "SELECT id FROM campaigns WHERE split_id=? AND family=?", (split_id, family)))
        registered = self.store.query(
            "SELECT COUNT(*) AS n FROM experiments e JOIN hypotheses h ON h.id = json_extract(e.notes, '$.hypothesis') "
            "WHERE e.kind='registered' AND e.status != 'blocked' AND h.split_id=? AND h.family=?",
            (split_id, family))[0]["n"]
        return sessions + evals + registered

    def quota(self, paths: Paths, split_id: str, family: str) -> dict[str, Any]:
        quota = (EXPLORATION_SHARE * FAMILY_QUOTA * len(sleeve_families(paths)) if family == EXPLORATION
                 else FAMILY_QUOTA)
        used = self.family_trials(split_id, family)
        return {"family": family, "split_id": split_id, "used": used, "quota": quota,
                "room": round(max(0.0, 1 - used / quota), 4) if quota else 0.0}

    def trials_left(self, paths: Paths, cid: str) -> int:
        """Trials the campaign may still count now: the lower of its own cap and its family's quota on the split."""
        row = self.require(cid)
        q = self.quota(paths, row["split_id"], row["family"])
        return max(0, math.floor(min(self.remaining(cid)["experiments"], q["quota"] - q["used"]) + 1e-9))

    def quotas(self, paths: Paths, split_id: str) -> list[dict[str, Any]]:
        return [self.quota(paths, split_id, f) for f in [*sleeve_families(paths), EXPLORATION]]

    def is_terminal(self, cid: str) -> bool:
        return self.require(cid)["state"] in TERMINAL
