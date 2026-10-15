"""The session brief of a campaign session (design 6.3, D4). Dev data and gate verdicts only."""

from __future__ import annotations

import json
from typing import Any

from quant_research.campaign.model import EXPLORATION, VERDICTS
from quant_research.campaign.store import CampaignStore
from quant_research.experiments.store import ExperimentStore
from quant_research.hypothesis.gates import verdict
from quant_research.memory.archive import archives_dir
from quant_research.paths import Paths
from quant_research.research.mandate import Mandate

ARCHIVE_CHARS = 8000


def _archive(paths: Paths, family: str) -> dict[str, Any] | None:
    p = archives_dir(paths) / f"{family}.json"
    return json.loads(p.read_text()) if p.exists() else None


def archive_view(md: str, product: str, split_id: str) -> str:
    """The archive markdown for one product: the title, the campaign's split first, the product's other splits, and
    the product's conclusions; cut at ARCHIVE_CHARS."""
    head, *sections = md.split("\n## ")
    own = [s for s in sections if s.startswith(f"{product} (`{split_id}`)")]
    other = [s for s in sections if s.startswith(f"{product} (") and s not in own]
    concl = [s for s in sections if s.startswith("Conclusions")]
    if concl:
        title, *items = concl[0].split("\n")
        concl = ["\n".join([title, *[i for i in items if f" {product} " in i or not i.startswith("- ")]])]
    return "\n## ".join([head.rstrip("\n") + "\n", *own, *other, *concl])[:ARCHIVE_CHARS]


def family_lines(paths: Paths, split_id: str, exclude: str | None) -> list[str]:
    """One line per family with an archive: thesis, trials and representative on this split, conclusions."""
    out = []
    for p in sorted(archives_dir(paths).glob("*.json")):
        a = json.loads(p.read_text())
        if a["family"] == exclude:
            continue
        v = a["splits"].get(split_id) or {}
        rep = v.get("representative") or {}
        out.append(f"- `{a['family']}` ({'sleeve' if a['sleeve'] else 'no sleeve'}): {a['thesis'][:160]} "
                   f"Trials here: {v.get('trials', 0)}; representative dev Sharpe {rep.get('sharpe', '-')}; "
                   f"conclusions {len(a['conclusions'])}.")
    return out


def session_brief(paths: Paths, store: ExperimentStore, cid: str, run_id: str, mandate: Mandate,
                  allowance: int, last: bool) -> str:
    cs = CampaignStore(store)
    row = cs.require(cid)
    spec = cs.spec(cid)
    caps, used = cs.caps(cid), cs.usage(cid)
    quota = cs.quota(paths, row["split_id"], row["family"])
    explore = row["family"] == EXPLORATION
    hyp = store.query("SELECT id FROM hypotheses WHERE campaign_id=?", (cid,))
    if hyp:
        v = verdict(store, hyp[0]["id"])
        hyp_line = (f"`{v['hypothesis_id']}` is registered; its gates: **{v['state']}**"
                    + (f" (failed gate {v['failed_gate']})" if v["failed_gate"] else "")
                    + ". No second registration is possible; write the conclusion unless a dev follow-up is "
                      "clearly worth the remaining budget.")
    elif explore:
        hyp_line = ("Exploration campaign: no registration (a new family is the human's decision). Report what "
                    "you find with evidence; a promising new family goes into the conclusion's open questions.")
    else:
        hyp_line = "None registered yet."
    notes = [f"- `{s['run_id']}`: {s['notes']}" for s in cs.sessions(cid) if s["notes"]]
    archive = None if explore else _archive(paths, row["family"])
    archive_md = archives_dir(paths) / f"{row['family']}.md"
    archive_txt = (archive_view(archive_md.read_text(), spec.product, row["split_id"])
                   if archive is not None and archive_md.exists()
                   else "(no archive written yet; `family_archive` may still return one later)")
    family_txt = (f"{EXPLORATION}: a family outside the current sleeves" if explore
                  else f"`{row['family']}`" + (f" ({archive['name']}): {archive['thesis']}" if archive else ""))
    cites = "\n".join(f"- {c.kind}: {c.ref}" for c in spec.citations)
    b = mandate.budget
    sessions_left = int(caps["sessions"] - used["sessions"])
    return f"""# Desk research session: campaign `{cid}`

research_id: `{run_id}`  task_id: `campaign-{cid}`

## Campaign

{spec.product}, hourly bars, long-or-cash spot. Family: {family_txt}

Goal: {spec.goal}

Prior: {spec.prior}

Citations:
{cites}

## Status

- Remaining campaign budget, this session included: {caps['usd'] - used['usd']:.2f} of {caps['usd']:.2f} USD,
  {sessions_left} of {int(caps['sessions'])} sessions,
  {int(caps['experiments'] - used['experiments'])} of {int(caps['experiments'])} dev experiments.
  This session may run {allowance} experiments.
- Family quota on this split: {quota['used']} of {quota['quota']:.0f} trials used.
- Hypothesis: {hyp_line}
{"- **This is the campaign's last session: write `conclusion` in research.json.**" if last else ""}

## Notes from earlier sessions of this campaign

{chr(10).join(notes) if notes else "- (first session)"}

## Family archive (dev data only)

{archive_txt}

## Other families

{chr(10).join(family_lines(paths, row["split_id"], None if explore else row["family"])) or "- (no archives yet)"}

## How the campaign works

- Every experiment is a trial on this split; marginal evaluations count as trials too. The reference portfolio is
  what new work is judged against: `portfolio_members` shows its members with dev statistics.
- `marginal_eval(experiment_id)` regresses a completed dev experiment's daily returns on the reference portfolio's
  dev returns (beta, alpha, appraisal ratio, per-period hedged Sharpe).
- At most one hypothesis per campaign, through `register_hypothesis` (sleeve family `{row['family']}` only): a
  prior, at least one citation (published work, or dev experiment ids on this split), and one to three variants
  (a template name or a `/workspace/*.py` file with exact parameters). The record is immutable. After this session
  the controller runs gate 1 (each variant alone on the full dev, regressed on the reference; the appraisal ratio
  must clear the bar for K = number of variants) and, if a variant passes, gate 2 (the reference with the variant
  added, on the validation segment you cannot see: no harm to Sharpe, drawdown or turnover). You get the verdicts
  only.
- The campaign ends when a session writes `conclusion`, or when a cap is reached.

## Budget (enforced)

- Experiments: {allowance} on the `dev` segment this session. Wall clock: {int(b.max_wall_seconds or 0)} s.

## Tools (MCP server `quant`)

- `inspect_dataset`, `submit_plan(plan)`, `submit_experiment(artifact, idempotency_key, parameters, plan_id)`,
  `get_experiment`, `compare_experiments`, `history` as before. `artifact` is a template name or a `.py` file under
  `/workspace/` implementing `/workspace/fixtures/strategy_contract.md`.
- `family_archive(family)`, `portfolio_members()`, `marginal_eval(experiment_id)`,
  `register_hypothesis(prior, citations, variants)`.
- `write_report("research.json")` validates your research.json.

There is no shell and no network besides these tools. Files you may write: anything under `/workspace/`.

## Deliverables

- `/workspace/research.json` following `/workspace/research.schema.json`; list every experiment id you ran.
  `final_artifact`: null (campaigns submit through `register_hypothesis`).
- `notes` (required): what this session did and found, and what the next session should do.
- `conclusion`: null to continue, or `{{"verdict": one of {list(VERDICTS)}, "summary", "evidence": [dev experiment
  ids], "open_questions": [...]}}` to end the campaign.
- `/workspace/report.md`: what you did, what the evidence supports and does not, all attempts, next step.
Call `write_report` before finishing and fix any problem it reports. The workspace is frozen when you finish.
"""
