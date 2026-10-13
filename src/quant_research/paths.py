from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """Runtime layout. `holdout` is a separate tree so research sandboxes can mount everything but it."""

    root: Path

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def holdout(self) -> Path:
        return self.root / "holdout"

    @property
    def splits(self) -> Path:
        return self.root / "splits"

    @property
    def configs(self) -> Path:
        return self.root / "configs"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def results(self) -> Path:
        return self.root / "results"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def sessions(self) -> Path:
        return self.root / "sessions"

    @property
    def paper(self) -> Path:
        return self.root / "paper"

    @property
    def db(self) -> Path:
        return self.root / "experiments.db"

    @property
    def readiness(self) -> Path:
        return self.root / "readiness.json"

    def ensure(self) -> Paths:
        for p in (
            self.raw,
            self.snapshots,
            self.holdout,
            self.splits,
            self.configs,
            self.artifacts,
            self.results,
            self.reports,
            self.sessions,
            self.paper,
        ):
            p.mkdir(parents=True, exist_ok=True)
        return self


def get_paths(root: str | Path | None = None) -> Paths:
    base = Path(root or os.environ.get("QUANT_HOME", "var")).resolve()
    return Paths(base).ensure()
