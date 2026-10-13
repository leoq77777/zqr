"""Candidate registry and lifecycle (docs/contracts-v2.md section 7).

SQLite rows plus immutable artifact directories. Every transition is checked against the allowed actor and
recorded with its evidence; rejected and failed states keep their evidence.
"""

from __future__ import annotations

import json
import shutil
from typing import Any, Literal

from quant_research import ENGINE_VERSION
from quant_research.experiments.store import ExperimentStore
from quant_research.hashing import hash_obj
from quant_research.paths import Paths
from quant_research.strategy.plugin import environment_lock, plugin_path
from quant_research.strategy.spec import StrategySpec

Actor = Literal["controller", "human", "evaluator", "paper"]

STATES = ("draft", "development", "validated", "frozen", "final_evaluated", "paper_approved", "paper_active",
          "retired", "rejected", "failed_final")
TERMINAL = {"retired", "rejected", "failed_final"}
TRANSITIONS: dict[tuple[str, str], set[str]] = {
    ("draft", "development"): {"controller", "human"},
    ("draft", "rejected"): {"controller", "human"},
    ("development", "validated"): {"controller"},  # deterministic mandate acceptance, never a model's opinion
    ("development", "rejected"): {"controller", "human"},
    ("validated", "frozen"): {"human"},
    ("validated", "rejected"): {"human"},
    ("frozen", "final_evaluated"): {"evaluator"},
    ("frozen", "failed_final"): {"evaluator"},
    ("final_evaluated", "paper_approved"): {"human"},
    ("paper_approved", "paper_active"): {"human", "paper"},
}


class RegistryError(RuntimeError):
    pass


def candidate_identity(spec: StrategySpec, data_hash: str, split_id: str, engine_config_id: str, policy_id: str,
                       mandate_id: str | None) -> dict[str, Any]:
    return {"spec": spec.identity(), "candidate_hash": spec.candidate_hash(), "strategy_hash": spec.strategy_hash(),
            "dependencies": environment_lock(), "data_hash": data_hash, "split_id": split_id,
            "engine_version": ENGINE_VERSION, "engine_config_id": engine_config_id, "policy_id": policy_id,
            "mandate_id": mandate_id}


def register_candidate(store: ExperimentStore, identity: dict[str, Any], *, session_id: str | None,
                       plan_id: str | None, actor: Actor, evidence: dict[str, Any]) -> str:
    """Create (or return) the candidate for this identity; it enters `development` when it has a plan."""
    ident_for_id = {k: v for k, v in identity.items() if k != "dependencies"}
    cid = "cand-" + hash_obj(ident_for_id)[:12]
    if store.get_candidate(cid) is None:
        spec = identity["spec"]
        store.insert_candidate({"id": cid, "state": "draft", "mandate_id": identity["mandate_id"],
                                "session_id": session_id, "plan_id": plan_id, "strategy_ref": spec["strategy_ref"],
                                "parameters": spec["parameters"], "candidate_hash": identity["candidate_hash"],
                                "identity": identity, "evidence": evidence})
        store.log_candidate_event(cid, None, "draft", actor, evidence)
        if plan_id:
            transition(store, cid, "development", actor, evidence, "plan referenced")
    return cid


def transition(store: ExperimentStore, cid: str, to: str, actor: Actor, evidence: dict[str, Any] | None = None,
               note: str = "") -> dict[str, Any]:
    cand = store.get_candidate(cid)
    if cand is None:
        raise RegistryError(f"unknown candidate {cid}")
    cur = cand["state"]
    if to == "retired":
        if cur in TERMINAL:
            raise RegistryError(f"candidate is already {cur}")
        if actor != "human":
            raise RegistryError("only a human can retire a candidate")
    elif actor not in TRANSITIONS.get((cur, to), set()):
        allowed = TRANSITIONS.get((cur, to))
        raise RegistryError(f"{cur} -> {to} is not allowed for {actor}" if allowed
                            else f"illegal transition {cur} -> {to}")
    store.update_candidate(cid, state=to, evidence=evidence or cand["evidence"])
    store.log_candidate_event(cid, cur, to, actor, evidence, note)
    return store.get_candidate(cid)


def freeze(paths: Paths, store: ExperimentStore, cid: str, note: str) -> dict[str, Any]:
    """validated -> frozen (human): copy code, identity and evidence into a read-only artifact directory."""
    cand = store.get_candidate(cid)
    if cand is None:
        raise RegistryError(f"unknown candidate {cid}")
    if cand["state"] != "validated":
        raise RegistryError(f"only validated candidates can be frozen (state {cand['state']})")
    out = paths.artifacts / "candidates" / cid
    out.mkdir(parents=True, exist_ok=True)
    spec = StrategySpec(strategy_ref=cand["strategy_ref"], parameters=cand["parameters"])
    if spec.kind == "plugin":
        shutil.copyfile(plugin_path(paths, spec.name), out / "strategy.py")
    for name, obj in (("identity.json", cand["identity"]), ("evidence.json", cand["evidence"]),
                      ("events.json", store.candidate_events(cid))):
        (out / name).write_text(json.dumps(obj, indent=2, default=str))
    for f in out.iterdir():
        f.chmod(0o444)
    store.update_candidate(cid, artifact_dir=str(out))
    store.freeze_candidate(cand["candidate_hash"], cand["strategy_ref"], cand["parameters"], note)
    return transition(store, cid, "frozen", "human", {"artifact_dir": str(out), **(cand["evidence"] or {})}, note)
