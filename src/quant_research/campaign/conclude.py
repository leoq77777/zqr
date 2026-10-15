"""The end of a campaign (design 6.3, D6): its conclusion, checked and appended to the family archive."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from quant_research.campaign.model import EXPLORATION, Conclusion
from quant_research.campaign.store import CampaignStore
from quant_research.experiments.store import ExperimentStore
from quant_research.hypothesis.gates import verdict
from quant_research.memory.archive import append_conclusion
from quant_research.memory.conclusions import add_campaign_conclusion
from quant_research.paths import Paths


def parse_conclusion(research: dict[str, Any] | None) -> tuple[Conclusion | None, list[str]]:
    raw = (research or {}).get("conclusion")
    if raw is None:
        return None, []
    try:
        return Conclusion.model_validate(raw), []
    except ValidationError as exc:
        return None, [f"conclusion: {e['loc']}: {e['msg']}" for e in exc.errors()]


def campaign_hypothesis(store: ExperimentStore, cid: str) -> str | None:
    rows = store.query("SELECT id FROM hypotheses WHERE campaign_id=?", (cid,))
    return rows[0]["id"] if rows else None


def finish_campaign(paths: Paths, store: ExperimentStore, cid: str, state: str, conclusion: Conclusion | None,
                    reason: str) -> dict[str, Any]:
    """`concluded` or `exhausted`. Without a conclusion from a session the controller writes an inconclusive one
    from the last session's notes, so every campaign leaves a record."""
    cs = CampaignStore(store)
    row = cs.require(cid)
    hid = campaign_hypothesis(store, cid)
    written_by = "session"
    if conclusion is None:
        notes = [s["notes"] for s in cs.sessions(cid) if s["notes"]]
        conclusion = Conclusion(verdict="inconclusive",
                                summary=f"No conclusion written by the sessions ({reason}). Last notes: "
                                        f"{notes[-1] if notes else '(none)'}"[:4000])
        written_by = "controller"
    body = {"topic": "campaign", "campaign_id": cid, "goal": row["body"]["goal"], "hypothesis": conclusion.summary,
            "decision": conclusion.verdict, "unresolved": list(conclusion.open_questions),
            "experiment_ids": list(conclusion.evidence), "limitations": [], "results": None,
            "registered": hid, "gates": verdict(store, hid)["state"] if hid else None, "end_state": state,
            "reason": reason, "written_by": written_by}
    family = None if row["family"] == EXPLORATION else row["family"]
    rec = add_campaign_conclusion(store, cid, family, row["product"], row["split_id"], body)
    appended = append_conclusion(paths, family, rec) if family else False
    cs.transition(cid, state, "controller", data={"conclusion_id": rec["id"], "citations_ok": bool(rec["citations_ok"]),
                                                  "archive_appended": appended, "reason": reason})
    return {"campaign_id": cid, "state": state, "conclusion_id": rec["id"], "citations_ok": bool(rec["citations_ok"]),
            "problems": rec["problems"], "archive_appended": appended, "written_by": written_by}
