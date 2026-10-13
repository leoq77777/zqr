"""Seed catalog (`seeds.yaml`, reviewed in docs/library/seeds.md): families and template entries with fixed
parameters, instantiated once per product."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from quant_research.hashing import hash_obj
from quant_research.strategy.spec import StrategySpec
from quant_research.strategy.templates import get_template

ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


class LibraryError(ValueError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Family(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]*$")
    name: str
    thesis: str
    sleeve: bool


class Entry(_Model):
    id: str = Field(pattern=ID_PATTERN)
    family: str
    kind: Literal["member", "overlay"]
    template: str
    parameters: dict[str, Any]
    sources: list[str] = Field(min_length=1)
    rationale: str
    notes: list[str] = Field(default_factory=list)

    def spec(self) -> StrategySpec:
        return StrategySpec(strategy_ref=f"template:{self.template}", parameters=self.parameters).normalized()


class Instance(_Model):
    """One catalog entry on one product: the unit stored in the library."""

    id: str
    entry: str
    product: str
    family: str
    kind: str
    strategy_ref: str
    parameters: dict[str, Any]
    template_version: str
    candidate_hash: str
    meta: dict[str, Any]

    def identity(self) -> dict[str, Any]:
        return {"entry": self.entry, "product": self.product, "family": self.family, "kind": self.kind,
                "strategy_ref": self.strategy_ref, "parameters": self.parameters,
                "template_version": self.template_version}

    def identity_hash(self) -> str:
        return hash_obj(self.identity())


class Catalog(_Model):
    version: int
    products: list[str] = Field(min_length=1)
    granularity: str
    max_member_share: float = Field(gt=0, le=1)
    families: list[Family]
    entries: list[Entry]

    @model_validator(mode="after")
    def _check(self):
        problems: list[str] = []
        fams = {f.id: f for f in self.families}
        if len(fams) != len(self.families):
            problems.append("duplicate family ids")
        ids = [e.id for e in self.entries]
        problems += [f"duplicate entry id {i!r}" for i in sorted({i for i in ids if ids.count(i) > 1})]
        for e in self.entries:
            fam = fams.get(e.family)
            if fam is None:
                problems.append(f"{e.id}: unknown family {e.family!r}")
                continue
            if (e.kind == "member") != fam.sleeve:
                problems.append(f"{e.id}: kind {e.kind!r} does not match family {fam.id!r} "
                                f"({'sleeve' if fam.sleeve else 'no sleeve'})")
            try:
                get_template(e.template)
                e.spec()
            except (KeyError, ValueError) as exc:
                problems.append(f"{e.id}: {exc}")
        problems += self._structure_problems(fams)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _structure_problems(self, fams: dict[str, Family]) -> list[str]:
        sleeves = [f for f in self.families if f.sleeve]
        problems = []
        for f in sleeves:
            n = sum(1 for e in self.entries if e.family == f.id)
            if n < 2:
                problems.append(f"family {f.id!r} takes a sleeve and needs at least 2 variants, has {n}")
            elif self.member_share(f.id) > self.max_member_share + 1e-12:
                problems.append(f"family {f.id!r}: member share {self.member_share(f.id):.4f} exceeds "
                                f"the cap {self.max_member_share}")
        return problems

    def member_share(self, family: str) -> float:
        """Capital share of one member: equal sleeves across families, equal weight within a family."""
        n_sleeves = sum(1 for f in self.families if f.sleeve)
        n = sum(1 for e in self.entries if e.family == family)
        return 1.0 / n_sleeves / n

    def catalog_hash(self) -> str:
        return hash_obj(self.model_dump())

    def instances(self) -> list[Instance]:
        out = []
        for product in self.products:
            base = product.split("-")[0].lower()
            for e in self.entries:
                spec = e.spec()
                out.append(Instance(
                    id=f"{base}-{e.id}", entry=e.id, product=product, family=e.family, kind=e.kind,
                    strategy_ref=spec.strategy_ref, parameters=spec.parameters,
                    template_version=get_template(e.template).version, candidate_hash=spec.candidate_hash(),
                    meta={"sources": e.sources, "rationale": e.rationale, "notes": e.notes,
                          "granularity": self.granularity}))
        return out


def default_catalog_path() -> Path:
    return Path(str(resources.files("quant_research.library").joinpath("seeds.yaml")))


def load_catalog(path: str | Path | None = None) -> Catalog:
    p = Path(path) if path else default_catalog_path()
    try:
        return Catalog.model_validate(yaml.safe_load(p.read_text()))
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise LibraryError(f"{p}: {exc}") from exc
