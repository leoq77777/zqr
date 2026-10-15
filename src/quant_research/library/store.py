"""Library tables in the experiment database. Entries are immutable: an import adds new ids and leaves existing
ones untouched; an existing id with a different identity refuses the whole import. Only the status changes, through
`set_status`, and every change is an event."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from quant_research.hashing import hash_obj
from quant_research.library.catalog import Catalog, Family, LibraryError
from quant_research.strategy.spec import StrategySpec
from quant_research.strategy.templates import get_template

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_families (
    id TEXT PRIMARY KEY, created_at REAL, name TEXT, thesis TEXT, sleeve INTEGER, source TEXT, catalog_hash TEXT
);
CREATE TABLE IF NOT EXISTS library_entries (
    id TEXT PRIMARY KEY, created_at REAL, updated_at REAL, entry TEXT, product TEXT, family TEXT, kind TEXT,
    status TEXT, source TEXT, strategy_ref TEXT, parameters TEXT, template_version TEXT, candidate_hash TEXT,
    identity_hash TEXT, catalog_hash TEXT, meta TEXT
);
CREATE TABLE IF NOT EXISTS library_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT, at REAL, from_status TEXT, to_status TEXT, actor TEXT,
    note TEXT
);
"""

STATUSES = ("candidate", "probation", "member", "retired")
TRANSITIONS = {"candidate": ("probation", "member", "retired"), "probation": ("member", "retired"),
               "member": ("retired",), "retired": ()}


class LibraryStore:
    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(Path(db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- import ----------------------------------------------------------------------------------
    def plan_import(self, catalog: Catalog) -> dict[str, Any]:
        """What an import would do, without writing. `conflicts` non-empty means it would be refused."""
        fams = {r["id"]: r for r in self._conn.execute("SELECT * FROM library_families")}
        entries = {r["id"]: r for r in self._conn.execute("SELECT * FROM library_entries")}
        plan: dict[str, Any] = {"catalog_hash": catalog.catalog_hash(), "families_added": [], "added": [],
                                "unchanged": [], "conflicts": []}
        for f in catalog.families:
            row = fams.get(f.id)
            if row is None:
                plan["families_added"].append(f.id)
            elif bool(row["sleeve"]) != f.sleeve:
                plan["conflicts"].append(f"family {f.id}: sleeve {bool(row['sleeve'])} in the library, "
                                         f"{f.sleeve} in the catalog")
        for inst in catalog.instances():
            row = entries.get(inst.id)
            if row is None:
                plan["added"].append(inst.id)
            elif row["identity_hash"] == inst.identity_hash():
                plan["unchanged"].append(inst.id)
            else:
                plan["conflicts"].append(f"{inst.id}: the catalog changes an imported entry; use a new entry id")
        return plan

    def import_catalog(self, catalog: Catalog, source: str = "seed", actor: str = "human") -> dict[str, Any]:
        plan = self.plan_import(catalog)
        if plan["conflicts"]:
            raise LibraryError("import refused: " + "; ".join(plan["conflicts"]))
        now = time.time()
        added_fams, added = set(plan["families_added"]), set(plan["added"])
        with self._conn:
            self._conn.execute("BEGIN")
            for f in catalog.families:
                if f.id in added_fams:
                    self._conn.execute("INSERT INTO library_families VALUES (?,?,?,?,?,?,?)",
                                       (f.id, now, f.name, f.thesis, int(f.sleeve), source, plan["catalog_hash"]))
            for inst in catalog.instances():
                if inst.id not in added:
                    continue
                self._conn.execute(
                    "INSERT INTO library_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (inst.id, now, now, inst.entry, inst.product, inst.family, inst.kind, "candidate", source,
                     inst.strategy_ref, json.dumps(inst.parameters, sort_keys=True), inst.template_version,
                     inst.candidate_hash, inst.identity_hash(), plan["catalog_hash"], json.dumps(inst.meta)))
                self._event(inst.id, now, None, "candidate", actor, f"imported from catalog {plan['catalog_hash']}")
        return plan

    # -- additions outside the catalog ----------------------------------------------------------
    def add_family(self, family: Family, note: str, actor: str = "human") -> dict[str, Any]:
        """A family confirmed by the human outside the seed catalog (design 4.5), so hypotheses can name it."""
        if not note.strip():
            raise LibraryError("a new family needs a note")
        if self._conn.execute("SELECT 1 FROM library_families WHERE id=?", (family.id,)).fetchone():
            raise LibraryError(f"family {family.id!r} already exists")
        self._conn.execute("INSERT INTO library_families VALUES (?,?,?,?,?,?,?)",
                           (family.id, time.time(), family.name, family.thesis, int(family.sleeve),
                            f"{actor}: {note}", None))
        return dict(self._conn.execute("SELECT * FROM library_families WHERE id=?", (family.id,)).fetchone())

    def add_candidate(self, product: str, family: str, strategy_ref: str, parameters: dict[str, Any],
                      source: str, actor: str = "controller") -> str:
        """A sleeve member from a registered hypothesis (gate 2), status `candidate`; idempotent on its identity."""
        fam = self._conn.execute("SELECT * FROM library_families WHERE id=?", (family,)).fetchone()
        if fam is None or not fam["sleeve"]:
            raise LibraryError(f"{family!r} is not a sleeve family in the library")
        spec = StrategySpec(strategy_ref=strategy_ref, parameters=parameters).normalized()
        name = f"{family}-h{hash_obj({'family': family, **spec.identity()})[:8]}"
        eid = f"{product.split('-')[0].lower()}-{name}"
        template_version = get_template(spec.name).version if spec.kind == "template" else None
        identity = {"entry": name, "product": product, "family": family, "kind": "member",
                    "strategy_ref": spec.strategy_ref, "parameters": spec.parameters,
                    "template_version": template_version}
        row = self.entry(eid)
        if row is not None:
            if row["identity_hash"] != hash_obj(identity):
                raise LibraryError(f"{eid}: an entry with this id has a different identity")
            return eid
        now = time.time()
        with self._conn:
            self._conn.execute("BEGIN")
            self._conn.execute("INSERT INTO library_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (eid, now, now, name, product, family, "member", "candidate", source,
                                spec.strategy_ref, json.dumps(spec.parameters, sort_keys=True), template_version,
                                spec.candidate_hash(), hash_obj(identity), None, json.dumps({"source": source})))
            self._event(eid, now, None, "candidate", actor, source)
        return eid

    # -- status ----------------------------------------------------------------------------------
    def set_status(self, entry_id: str, to: str, actor: str, note: str) -> None:
        row = self.entry(entry_id)
        if row is None:
            raise LibraryError(f"unknown library entry {entry_id!r}")
        if to not in TRANSITIONS[row["status"]]:
            raise LibraryError(f"{entry_id}: {row['status']} -> {to} is not allowed")
        now = time.time()
        with self._conn:
            self._conn.execute("BEGIN")
            self._conn.execute("UPDATE library_entries SET status=?, updated_at=? WHERE id=?", (to, now, entry_id))
            self._event(entry_id, now, row["status"], to, actor, note)

    def _event(self, entry_id: str, at: float, frm: str | None, to: str, actor: str, note: str) -> None:
        self._conn.execute("INSERT INTO library_events (entry_id, at, from_status, to_status, actor, note) "
                           "VALUES (?,?,?,?,?,?)", (entry_id, at, frm, to, actor, note))

    # -- queries ---------------------------------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        for k in ("parameters", "meta"):
            if d.get(k) is not None:
                d[k] = json.loads(d[k])
        return d

    def entry(self, entry_id: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM library_entries WHERE id=?", (entry_id,)).fetchone()
        return self._row(r) if r else None

    def entries(self, product: str | None = None, family: str | None = None,
                status: str | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        for col, val in (("product", product), ("family", family), ("status", status)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        sql = "SELECT * FROM library_entries" + (" WHERE " + " AND ".join(where) if where else "")
        return [self._row(r) for r in self._conn.execute(sql + " ORDER BY product, family, id", args)]

    def families(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM library_families ORDER BY id")]

    def events(self, entry_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM library_events WHERE entry_id=? ORDER BY id", (entry_id,))]
