"""The campaign body (design 6.1) and its caps (proposal P3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from quant_research.hypothesis.model import Citation

EXPLORATION = "exploration"
CAPS_START = {"usd": 1.0, "sessions": 4, "experiments": 40}
TOPUP = {"usd": 2.0, "sessions": 2, "experiments": 20}
CAPS_MAX = {"usd": 5.0, "sessions": 8, "experiments": 80}
TOPUP_REASONS = ("gate1", "gate2")
STATES = ("proposed", "queued", "running", "concluded", "exhausted", "cancelled")
TERMINAL = ("concluded", "exhausted", "cancelled")
VERDICTS = ("supported", "refuted", "inconclusive")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CampaignSpec(_Model):
    product: str
    family: str  # a sleeve family of the library, or `exploration`
    goal: str = Field(min_length=10, max_length=1000)
    prior: str = Field(min_length=1, max_length=2000)
    citations: tuple[Citation, ...] = Field(min_length=1)
    model: str | None = None  # None: the desk's configured model


class Conclusion(_Model):
    """What a campaign's last session writes under `conclusion` in research.json (design 6.1)."""

    verdict: Literal["supported", "refuted", "inconclusive"]
    summary: str = Field(min_length=1, max_length=4000)
    evidence: tuple[str, ...] = ()  # dev experiment ids on the campaign's split
    open_questions: tuple[str, ...] = ()
