"""Family tags and the cluster-to-family map (appendix A12 point 4).

Seeds carry their catalog family. Old trials carry their session topic: `volatility_filter` maps to `vol_overlay`,
`mean_reversion` to `short_term_reversal` (archive only), `trend` and `multi_timeframe` to no family (their cluster's
seeds place them). Clusters are mapped at residual correlation 0.3: a cluster containing seeds takes the seeds'
majority family (ties: the family of the lowest-id seed); otherwise a family needs more than half of the cluster's
trials. Unmapped clusters of at least `MIN_PROPOSAL` trials become new-family proposals for the human.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

from quant_research.experiments.store import ExperimentStore
from quant_research.library.catalog import Catalog, load_catalog
from quant_research.memory.clusters import FLAT, PASSIVE, members
from quant_research.memory.schema import ensure
from quant_research.paths import Paths

LEGACY_TOPICS: dict[str, str | None] = {"volatility_filter": "vol_overlay", "mean_reversion": "short_term_reversal",
                                        "trend": None, "multi_timeframe": None}
MAPPING_THRESHOLD = 0.3
MIN_PROPOSAL = 5


def session_topics(paths: Paths) -> dict[str, dict[str, str]]:
    """session id -> {product, topic, run_id} from the desk research ledger."""
    ledger = paths.desk / "research_ledger.jsonl"
    out: dict[str, dict[str, str]] = {}
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            row = json.loads(line)
            if row.get("session_id"):
                out[row["session_id"]] = {"product": row.get("product"), "topic": row.get("topic"),
                                          "run_id": row.get("run_id")}
    return out


def _seed_entry(notes: Any) -> str | None:
    try:
        return (json.loads(notes) if isinstance(notes, str) else notes or {}).get("library_entry")
    except (json.JSONDecodeError, AttributeError):
        return None


def tag_trials(paths: Paths, store: ExperimentStore, catalog: Catalog | None = None) -> Counter:
    """Fill `return_index.family_tag` / `tag_source`; returns counts by source."""
    ensure(store)
    catalog = catalog or load_catalog()
    family_of = {e.id: e.family for e in catalog.entries}
    topics = session_topics(paths)
    counts: Counter = Counter()
    rows = store.query("SELECT r.experiment_id, r.kind, r.session_id, e.notes FROM return_index r "
                       "JOIN experiments e ON e.id = r.experiment_id")
    for r in rows:
        tag, source = None, None
        entry = _seed_entry(r["notes"]) if r["kind"] == "seed" else None
        if entry:
            tag, source = family_of.get(entry), f"seed:{entry}"
        elif r["session_id"] in topics:
            topic = topics[r["session_id"]]["topic"]
            tag, source = LEGACY_TOPICS.get(topic), f"topic:{topic}"
        store.execute("UPDATE return_index SET family_tag=?, tag_source=? WHERE experiment_id=?",
                      (tag, source, r["experiment_id"]))
        counts[(source or "untagged").split(":")[0]] += 1
    return counts


def map_clusters(store: ExperimentStore, clustering_id: str,
                 threshold: float = MAPPING_THRESHOLD) -> dict[str, dict[str, Any]]:
    """label -> {families, shared, basis, size, members}; special labels (flat, passive, no returns) are not mapped.

    Seeds decide; when several families tie for the most seeds, the cluster maps to all of them (shared, A13.2).
    Without seeds, a family needs a strict majority of the cluster's tagged trials.
    """
    lab = members(store, clustering_id, threshold)
    info = {r["experiment_id"]: r for r in store.query(
        "SELECT experiment_id, kind, family_tag, tag_source, no_returns FROM return_index WHERE experiment_id IN "
        "(SELECT experiment_id FROM cluster_members WHERE clustering_id=? AND threshold=?)",
        (clustering_id, threshold))}
    groups: dict[str, list[str]] = {}
    for exp_id, label in lab.items():
        if label in (FLAT, PASSIVE) or info.get(exp_id, {}).get("no_returns"):
            continue
        groups.setdefault(label, []).append(exp_id)
    out: dict[str, dict[str, Any]] = {}
    for label, ids in sorted(groups.items()):
        seeds = [i for i in ids if info[i]["kind"] == "seed" and info[i]["family_tag"]]
        fams: list[str] = []
        basis = None
        if seeds:
            votes = Counter(info[i]["family_tag"] for i in seeds)
            top = max(votes.values())
            fams, basis = sorted(f for f, n in votes.items() if n == top), "seed"
        else:
            votes = Counter(info[i]["family_tag"] for i in ids if info[i]["family_tag"])
            if votes:
                fam, n = votes.most_common(1)[0]
                if n * 2 > len(ids):
                    fams, basis = [fam], "tags"
        out[label] = {"families": fams, "shared": len(fams) > 1, "basis": basis, "size": len(ids), "members": ids}
    return out


def propose(store: ExperimentStore, split_id: str, clustering_id: str,
            mapping: dict[str, dict[str, Any]]) -> list[str]:
    """New-family proposals for unmapped clusters; a proposal for the same members is not repeated."""
    created = []
    for label, m in mapping.items():
        if m["families"] or m["size"] < MIN_PROPOSAL:
            continue
        ids = m["members"]
        marks = ",".join("?" * len(ids))
        rows = store.query(f"SELECT r.experiment_id, r.sharpe, r.tag_source, e.strategy_ref, e.parameters "
                           f"FROM return_index r JOIN experiments e ON e.id = r.experiment_id "
                           f"WHERE r.experiment_id IN ({marks})", ids)
        best = max(rows, key=lambda r: r["sharpe"] if r["sharpe"] is not None else float("-inf"))
        sharpes = sorted(r["sharpe"] for r in rows if r["sharpe"] is not None)
        body = {"label": label, "size": len(ids), "members": ids,
                "topics": dict(Counter(r["tag_source"] or "untagged" for r in rows)),
                "strategies": dict(Counter(r["strategy_ref"] for r in rows).most_common(5)),
                "representative": {"experiment_id": best["experiment_id"], "sharpe": best["sharpe"],
                                   "strategy_ref": best["strategy_ref"], "parameters": json.loads(best["parameters"])},
                "median_sharpe": sharpes[len(sharpes) // 2] if sharpes else None}
        pid = f"prop-{split_id}-{min(ids)}-{len(ids)}"
        if store.execute("INSERT OR IGNORE INTO family_proposals (id, clustering_id, split_id, label, body, status, "
                         "decided_at) VALUES (?,?,?,?,?,?,?)",
                         (pid, clustering_id, split_id, label, json.dumps(body), "open", None)):
            created.append(pid)
    return created


def proposals(store: ExperimentStore, split_id: str | None = None,
              status: str | None = "open") -> list[dict[str, Any]]:
    """Proposals with the given status (all when None); decided ones carry their decision's reason."""
    ensure(store)
    where, args = ["1=1"], []
    if status:
        where, args = [*where, "status=?"], [*args, status]
    if split_id:
        where, args = [*where, "split_id=?"], [*args, split_id]
    reasons = {r["proposal_id"]: r["reason"] for r in store.query(
        "SELECT proposal_id, reason FROM proposal_decisions ORDER BY id")}
    return [r | {"body": json.loads(r["body"]), "reason": reasons.get(r["id"])}
            for r in store.query(f"SELECT * FROM family_proposals WHERE {' AND '.join(where)} ORDER BY id", args)]


def reject_proposal(store: ExperimentStore, proposal_id: str, reason: str) -> None:
    """The human's rejection: the cluster stays unmapped and is not proposed again for the same members."""
    ensure(store)
    if not reason.strip():
        raise ValueError("a rejection needs a reason")
    rows = store.query("SELECT status FROM family_proposals WHERE id=?", (proposal_id,))
    if not rows:
        raise ValueError(f"unknown proposal {proposal_id}")
    if rows[0]["status"] != "open":
        raise ValueError(f"proposal {proposal_id} is already {rows[0]['status']}")
    now = time.time()
    store.execute("INSERT INTO proposal_decisions (proposal_id, status, reason, decided_at) VALUES (?,?,?,?)",
                  (proposal_id, "rejected", reason, now))
    store.execute("UPDATE family_proposals SET status='rejected', decided_at=? WHERE id=?", (now, proposal_id))
