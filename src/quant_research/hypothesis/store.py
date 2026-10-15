"""Registered hypotheses in the experiment database (appendix A6, A13.7).

A hypothesis is registered once and never changes: the body, the split, the desk mandate and gate 1's reference
are fixed at registration. Gate results are appended as events; the state is derived from them.
"""

from __future__ import annotations

import json
import time
from typing import Any

from quant_research.desk.config import load_config
from quant_research.experiments.store import ExperimentStore
from quant_research.hashing import hash_obj
from quant_research.hypothesis.model import Hypothesis
from quant_research.library.store import LibraryStore
from quant_research.paths import Paths
from quant_research.portfolio.evaluate import gate1_reference
from quant_research.strategy.plugin import plugin_path
from quant_research.strategy.spec import StrategyError

SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY, product TEXT NOT NULL, split_id TEXT NOT NULL, mandate_id TEXT NOT NULL,
    reference_id TEXT NOT NULL, family TEXT NOT NULL, campaign_id TEXT, body TEXT NOT NULL,
    registered_by TEXT NOT NULL, note TEXT, at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS hypotheses_campaign ON hypotheses(campaign_id) WHERE campaign_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS hypothesis_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, hypothesis_id TEXT NOT NULL, event TEXT NOT NULL, actor TEXT NOT NULL,
    evidence TEXT, at REAL NOT NULL);
"""

STATES = ("registered", "gate1_failed", "gate2_failed", "admitted")


class HypothesisError(ValueError):
    pass


class HypothesisStore:
    def __init__(self, store: ExperimentStore):
        self.store = store
        store.ensure_schema(SCHEMA)

    # -- registration ------------------------------------------------------------------------------
    def register(self, paths: Paths, hypothesis: Hypothesis, registered_by: str, note: str = "") -> dict[str, Any]:
        product = hypothesis.product
        mandate_id = load_config(paths).mandates.get(product)
        if mandate_id is None:
            raise HypothesisError(f"no desk mandate for {product}")
        split_id = self.store.get_mandate(mandate_id)["body"]["split_id"]
        reference = gate1_reference(self.store, product, split_id)
        if reference is None:
            raise HypothesisError(f"no gate 1 reference for {product} on {split_id} "
                                  "(`zqr portfolio dev <vid> --reference`)")
        self._check_family(paths, hypothesis.family)
        variants = self._normalized_variants(paths, hypothesis)
        self._check_citations(hypothesis, split_id)
        if hypothesis.campaign_id and self.store.query("SELECT id FROM hypotheses WHERE campaign_id=?",
                                                       (hypothesis.campaign_id,)):
            raise HypothesisError(f"campaign {hypothesis.campaign_id} already registered a hypothesis")
        body = hypothesis.model_copy(update={"variants": variants}).model_dump(mode="json")
        hid = "hy-" + hash_obj({"body": body, "split_id": split_id, "reference_id": reference})[:12]
        if self.get(hid) is not None:
            raise HypothesisError(f"this hypothesis is already registered as {hid}")
        now = time.time()
        self.store.execute("INSERT INTO hypotheses VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (hid, product, split_id, mandate_id, reference, hypothesis.family,
                            hypothesis.campaign_id, json.dumps(body, sort_keys=True), registered_by, note, now))
        self.record(hid, "registered", registered_by, {"split_id": split_id, "reference_id": reference})
        return self.get(hid)

    @staticmethod
    def _check_family(paths: Paths, family: str) -> None:
        lib = LibraryStore(paths.db)
        try:
            fams = {f["id"]: f for f in lib.families()}
        finally:
            lib.close()
        if family not in fams:
            raise HypothesisError(f"unknown family {family!r} (a new family is added by the human: "
                                  "`zqr library family add`)")
        if not fams[family]["sleeve"]:
            raise HypothesisError(f"family {family!r} has no sleeve; variants must be sleeve members")

    @staticmethod
    def _normalized_variants(paths: Paths, hypothesis: Hypothesis) -> tuple:
        out, seen = [], set()
        for i, v in enumerate(hypothesis.variants):
            try:
                spec = v.spec()
                if spec.kind == "plugin":
                    plugin_path(paths, spec.name)
            except (StrategyError, KeyError, ValueError) as exc:
                raise HypothesisError(f"variant {i + 1}: {exc}") from None
            key = spec.candidate_hash()
            if key in seen:
                raise HypothesisError(f"variant {i + 1} repeats an earlier variant")
            seen.add(key)
            out.append(v.model_copy(update={"parameters": spec.parameters}))
        return tuple(out)

    def _check_citations(self, hypothesis: Hypothesis, split_id: str) -> None:
        splits = [split_id, *self.store.split_ancestors(split_id)]
        for c in hypothesis.citations:
            if c.kind != "experiment":
                continue
            row = self.store.get_experiment(c.ref)
            if row is None:
                raise HypothesisError(f"cited experiment {c.ref} does not exist")
            if row["split_id"] not in splits or row["segment"] != "dev":
                raise HypothesisError(f"cited experiment {c.ref} is not a dev experiment on {split_id} or the "
                                      "splits it extends")

    # -- events and queries ------------------------------------------------------------------------
    def record(self, hid: str, event: str, actor: str, evidence: dict[str, Any] | None = None) -> None:
        self.store.execute("INSERT INTO hypothesis_events (hypothesis_id, event, actor, evidence, at) "
                           "VALUES (?,?,?,?,?)",
                           (hid, event, actor, json.dumps(evidence, sort_keys=True, default=str) if evidence
                            else None, time.time()))

    def events(self, hid: str) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM hypothesis_events WHERE hypothesis_id=? ORDER BY id", (hid,))
        return [r | {"evidence": json.loads(r["evidence"]) if r["evidence"] else None} for r in rows]

    def event(self, hid: str, name: str) -> dict[str, Any] | None:
        found = [e for e in self.events(hid) if e["event"] == name]
        return found[-1] if found else None

    def state(self, hid: str) -> str:
        state = "registered"
        for e in self.events(hid):
            passed = (e["evidence"] or {}).get("passed")
            if e["event"] == "gate1" and not passed:
                state = "gate1_failed"
            elif e["event"] == "gate2":
                state = "admitted" if passed else "gate2_failed"
        return state

    def get(self, hid: str) -> dict[str, Any] | None:
        rows = self.store.query("SELECT * FROM hypotheses WHERE id=?", (hid,))
        return self._row(rows[0]) if rows else None

    def hypothesis(self, hid: str) -> Hypothesis:
        row = self.get(hid)
        if row is None:
            raise HypothesisError(f"unknown hypothesis {hid}")
        return Hypothesis.model_validate(row["body"])

    def list(self, product: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM hypotheses" + (" WHERE product=?" if product else "") +
                                " ORDER BY at, id", (product,) if product else ())
        out = [self._row(r) for r in rows]
        return [r for r in out if state is None or r["state"] == state]

    def _row(self, r: dict[str, Any]) -> dict[str, Any]:
        return r | {"body": json.loads(r["body"]), "state": self.state(r["id"])}
