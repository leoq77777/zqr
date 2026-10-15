"""Session conclusions, append-only (design 4.6): the import of the desk's session reports.

Each `sessions/<sid>/research.json` becomes one `family_conclusions` row, checked with the gateway's report
validation (experiment ids, artifact hashes, cited numbers). Reports that fail the check are kept with their
problems. Rows are never updated or deleted; a session is imported once.
"""

from __future__ import annotations

import json
import time
from typing import Any

from quant_research.experiments.store import ExperimentStore
from quant_research.gateway.service import GatewayError, PolicyRefusal, ToolGateway
from quant_research.memory.families import session_family, session_topics
from quant_research.memory.schema import ensure
from quant_research.paths import Paths

KEPT = ("hypothesis", "preregistered_plan", "decision", "limitations", "unresolved", "experiment_ids", "results")


def import_conclusions(paths: Paths, store: ExperimentStore) -> dict[str, int]:
    ensure(store)
    topics = session_topics(paths)
    known = {r["session_id"] for r in store.query("SELECT session_id FROM family_conclusions "
                                                  "WHERE session_id IS NOT NULL")}
    counts = {"imported": 0, "present": 0, "citation_failures": 0}
    for report in sorted(paths.sessions.glob("*/research.json")):
        sid = report.parent.name
        if sid in known:
            counts["present"] += 1
            continue
        try:
            doc = json.loads(report.read_text())
        except json.JSONDecodeError as exc:
            doc, problems = {}, [f"research.json is not valid JSON: {exc}"]
        else:
            try:
                gw = ToolGateway(paths, store, sid, caller="controller", workspace=report.parent)
                problems = gw.build_report("research.json", record=False)["problems"]
            except (GatewayError, PolicyRefusal) as exc:
                problems = [str(exc)]
        meta = topics.get(sid, {})
        body = {k: doc.get(k) for k in KEPT} | {"topic": meta.get("topic"), "run_id": meta.get("run_id")}
        if meta.get("campaign_id"):
            body |= {"campaign_id": meta["campaign_id"], "notes": doc.get("notes")}
        store.execute("INSERT OR IGNORE INTO family_conclusions (family_id, product, source, session_id, body, "
                      "citations_ok, problems, created_at) VALUES (?,?,?,?,?,?,?,?)",
                      (session_family(meta), meta.get("product"), "session", sid,
                       json.dumps(body, ensure_ascii=False), int(not problems), json.dumps(problems), time.time()))
        counts["imported"] += 1
        counts["citation_failures"] += bool(problems)
    return counts


def evidence_problems(store: ExperimentStore, split_id: str, experiment_ids: list[str]) -> list[str]:
    """Cited experiments must exist and be dev runs on the split or the splits it extends."""
    splits = [split_id, *store.split_ancestors(split_id)]
    problems = []
    for exp_id in dict.fromkeys(experiment_ids):
        rec = store.get_experiment(exp_id) if isinstance(exp_id, str) else None
        if rec is None:
            problems.append(f"unknown experiment {exp_id!r}")
        elif rec["split_id"] not in splits or rec["segment"] != "dev":
            problems.append(f"{exp_id} is not a dev experiment on {split_id}")
    return problems


def add_campaign_conclusion(store: ExperimentStore, campaign_id: str, family: str | None, product: str,
                            split_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """The campaign's conclusion (design 6.3), once per campaign; its citations are checked here."""
    ensure(store)
    problems = evidence_problems(store, split_id, list(body.get("experiment_ids") or []))
    key = f"campaign:{campaign_id}"
    store.execute("INSERT OR IGNORE INTO family_conclusions (family_id, product, source, session_id, body, "
                  "citations_ok, problems, created_at) VALUES (?,?,?,?,?,?,?,?)",
                  (family, product, "campaign", key, json.dumps(body, ensure_ascii=False, default=str),
                   int(not problems), json.dumps(problems), time.time()))
    row = store.query("SELECT * FROM family_conclusions WHERE session_id=?", (key,))[0]
    return row | {"body": json.loads(row["body"]), "problems": json.loads(row["problems"] or "[]")}


def conclusions(store: ExperimentStore) -> list[dict[str, Any]]:
    ensure(store)
    return [r | {"body": json.loads(r["body"]), "problems": json.loads(r["problems"] or "[]")}
            for r in store.query("SELECT * FROM family_conclusions ORDER BY id")]
