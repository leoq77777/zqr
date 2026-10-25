"""Standing authorization: a human pre-approves the desk to freeze validated candidates and put holdout-passing
ones on paper. Super mode (`auto_paper_admitted`) also puts Gate-2-admitted versions on paper immediately
(A13.12). It never covers live trading.

`desk/authorization.json` is written only by `zqr desk setup` / `zqr desk authorize` after an interactive
confirmation. Every desk transition records the file's hash as evidence.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from pydantic import BaseModel, Field, PositiveInt

from quant_research.hashing import hash_obj

DESK_TRANSITIONS = (("validated", "frozen"), ("final_evaluated", "paper_approved"))


class AuthorizationError(RuntimeError):
    pass


class Authorization(BaseModel):
    products: list[str] = Field(min_length=1)
    max_paper_strategies: PositiveInt
    created_at: float
    expires_at: float
    created_by: str = "human"
    note: str = ""
    auto_paper_admitted: bool = False  # A13.12: Gate 1+2 pass starts paper; no per-version human, no next split
    scope: list[str] = Field(default_factory=lambda: [f"{a}->{b}" for a, b in DESK_TRANSITIONS])
    hash: str = ""

    def body_hash(self) -> str:
        return hash_obj(self.model_dump(exclude={"hash"}))

    def sealed(self) -> Authorization:
        return self.model_copy(update={"hash": self.body_hash()})

    def check(self, now: float | None = None, transition: tuple[str, str] | None = None,
              product: str | None = None) -> None:
        now = time.time() if now is None else now
        if not self.hash or self.hash != self.body_hash():
            raise AuthorizationError("authorization hash does not match its content; re-run `zqr desk authorize`")
        if now >= self.expires_at:
            raise AuthorizationError("standing authorization has expired; renew with `zqr desk authorize`")
        if transition is not None and f"{transition[0]}->{transition[1]}" not in self.scope:
            raise AuthorizationError(f"{transition[0]} -> {transition[1]} is outside the authorization scope")
        if product is not None and product not in self.products:
            raise AuthorizationError(f"{product} is not an authorized product")


def create(products: list[str], max_paper_strategies: int, days: float, note: str = "",
           now: float | None = None, auto_paper_admitted: bool = False) -> Authorization:
    now = time.time() if now is None else now
    return Authorization(products=products, max_paper_strategies=max_paper_strategies, created_at=now,
                         expires_at=now + days * 86400, note=note,
                         auto_paper_admitted=auto_paper_admitted).sealed()


def save(path: Path, auth: Authorization) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(auth.model_dump_json(indent=2))


def load(path: Path) -> Authorization | None:
    if not Path(path).exists():
        return None
    return Authorization.model_validate(json.loads(Path(path).read_text()))
