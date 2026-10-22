"""Deterministic campaign proposals from dev diagnostics (design 6.2, D2). Dev data only.

Inputs: the reference portfolio's dev run without the ladder (yearly period statistics, `family_contribution`),
the written family archives, the conclusions and the family quotas (P1). Diagnostics:

- regime gap: a calendar year in which the reference lagged its own market exposure; gap = log(1 + mean exposure x
  buy and hold) - log(1 + total return), so bull years do not dominate; the years, largest gap first, go round the
  sleeve families ordered by quota room, then by smallest share of the reference's P&L;
- concentration: the largest family share of the reference's P&L above an equal share, or the mean residual
  correlation between the sleeves' representatives; an exploration campaign;
- cost: a family whose representative's dev Sharpe falls at doubled costs; gap = the relative fall;
- open question: an unresolved question in a conclusion of the family; gap 0.3. Dropped when the question needs
  Gate 2 / validation measures denied to agents (A6), or when it is a parameter retry to re-pass Gate 2 after that
  family already failed Gate 2 on the split (or an ancestor);
- unmapped cluster: an open new-family proposal of the clustering; exploration; gap = size / 50 (at most 1).

Score = gap x room left in the family's quota; ties by key. About a third of each batch is exploration. A proposal
whose key is already proposed, queued or running is not repeated.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from typing import Any

from quant_research.campaign.model import EXPLORATION, CampaignSpec
from quant_research.campaign.store import CampaignError, CampaignStore, desk_split, sleeve_families
from quant_research.experiments.store import ExperimentStore
from quant_research.hypothesis.gates import _reference_dev
from quant_research.memory.archive import archives_dir
from quant_research.memory.conclusions import conclusions
from quant_research.memory.families import proposals as cluster_proposals
from quant_research.paths import Paths
from quant_research.portfolio.evaluate import _summary, gate1_reference, version_periods
from quant_research.timeutil import parse_time

EXPLORATION_SHARE = 0.35
OPEN_QUESTION_GAP = 0.3

# A6: agents see only that Gate 2 failed, never which check or any validation measure.
_UNANSWERABLE = re.compile(
    r"(?i)"
    r"(?:which\s+gate[-\s]?2\s+component"
    r"|which\s+(?:of\s+)?(?:the\s+)?(?:three\s+)?(?:sharpe|drawdown|turnover)\b.{0,80}(?:fail|failed|failing)"
    r"|gate[-\s]?2\s+component\s+failed"
    r"|binding\s+gate[-\s]?2\s+constraint"
    r"|validation\s+(?:sharpe|drawdown|turnover|metric|measure)"
    r"|(?:sharpe|drawdown|turnover)\s+(?:on\s+)?(?:the\s+)?validation\b)"
)

# Open questions that treat Gate 2 as a retargetable objective for a parameter tweak.
_GATE2_RETRY = re.compile(
    r"(?i)"
    r"(?:survive|survives|pass|passes|passing|clear|clears)\s+(?:a\s+)?gate[-\s]?2"
    r"|gate[-\s]?2[-\s]?style"
    r"|pass\s+a\s+gate[-\s]?2"
    r"|would\s+.{0,200}\s+(?:survive|pass|clear)\s+gate[-\s]?2"
    r"|re-?(?:register|run|try|submit).{0,40}gate\s*2"
)


def _years(start: int, end: int) -> list[int]:
    y0, y1 = (datetime.fromtimestamp(t, tz=UTC).year for t in (start, end))
    return [parse_time(f"{y}-01-01") for y in range(y0 + 1, y1 + 1)]


def _archives(paths: Paths) -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text()) for p in sorted(archives_dir(paths).glob("*.json"))}


def _r(x: float) -> float:
    return round(float(x), 4)


def unanswerable_open_question(q: str) -> bool:
    """True when answering needs Gate 2 / validation internals denied to agents (A6)."""
    return bool(_UNANSWERABLE.search(q))


def gate2_retry_open_question(q: str) -> bool:
    """True when the question frames a variant/parameter tweak as another attempt to pass Gate 2."""
    return bool(_GATE2_RETRY.search(q))


def gate2_failed_families(store: ExperimentStore, split_id: str) -> set[str]:
    """Sleeve families with a Gate 2 failure on this split or an ancestor (A13.8 revalidations inherit)."""
    from quant_research.hypothesis.store import HypothesisStore

    splits = {split_id, *store.split_ancestors(split_id)}
    return {h["family"] for h in HypothesisStore(store).list()
            if h["split_id"] in splits and h["state"] == "gate2_failed"}


def open_question_allowed(q: str, family: str, failed: set[str]) -> bool:
    """Whether an unresolved conclusion question may become a campaign (dev-answerable, not a Gate 2 collision)."""
    if unanswerable_open_question(q):
        return False
    return not (family in failed and gate2_retry_open_question(q))


def diagnostics(paths: Paths, store: ExperimentStore, product: str) -> dict[str, Any]:
    split_id = desk_split(paths, store, product)
    ref = gate1_reference(store, product, split_id)
    if ref is None:
        raise CampaignError(f"no gate 1 reference for {product} on {split_id}")
    dev = _reference_dev(store, ref)
    periods = version_periods(paths, store, ref, _years(dev["window_start"], dev["window_end"]))
    return {"product": product, "split_id": split_id, "reference_id": ref, "reference_experiment": dev["id"],
            "years": [p for p in (periods["periods_no_ladder"] or periods["periods"]) if p["period"] == "part"],
            "family_contribution": _summary(dev).get("family_contribution") or {},
            "archives": _archives(paths), "sleeves": sleeve_families(paths)}


def candidates(paths: Paths, store: ExperimentStore, product: str) -> list[dict[str, Any]]:
    """Every proposal the diagnostics support, scored; not yet filtered for duplicates or the batch mix."""
    d = diagnostics(paths, store, product)
    cs = CampaignStore(store)
    split_id, sleeves, archives = d["split_id"], d["sleeves"], d["archives"]
    room = {f: cs.quota(paths, split_id, f)["room"] for f in [*sleeves, EXPLORATION]}
    ref_cite = {"kind": "experiment", "ref": d["reference_experiment"]}
    out: list[dict[str, Any]] = []

    def add(key: str, family: str, diagnostic: str, gap: float, goal: str, prior: str,
            cites: list[str] | None = None, data: dict[str, Any] | None = None) -> None:
        if gap <= 0 or room.get(family, 0) <= 0:
            return
        citations = [ref_cite] + [{"kind": "experiment", "ref": c} for c in cites or [] if c != ref_cite["ref"]]
        out.append({"key": key, "family": family, "diagnostic": diagnostic, "gap": _r(min(gap, 1.0)),
                    "room": room[family], "score": _r(min(gap, 1.0) * room[family]),
                    "spec": {"product": product, "family": family, "goal": goal, "prior": prior,
                             "citations": citations},
                    "data": data or {}})

    fc = {k: v for k, v in d["family_contribution"].items() if k != "none"}
    total = sum(abs(v) for v in fc.values())
    order = sorted(sleeves, key=lambda f: (-room[f], fc.get(f, 0.0), f))
    gaps = []
    for p in d["years"]:
        passive = p["exposure"] * p["buy_and_hold"]["total_return"]
        shortfall = math.log1p(passive) - math.log1p(p["total_return"])
        if shortfall > 0:
            gaps.append((shortfall, p, passive))
    for i, (shortfall, p, passive) in enumerate(sorted(gaps, key=lambda g: (-g[0], g[1]["from"]))):
        if not order:
            break
        fam, year = order[i % len(order)], p["from"][:4]
        add(f"regime:{year}", fam, "regime_gap", shortfall,
            f"Find a {fam} variant that holds up in {year}, when the reference portfolio returned "
            f"{p['total_return']:.1%} on dev against {passive:.1%} for its mean exposure to the market.",
            f"The reference's dev run without the ladder returned {p['total_return']:.1%} in {year} at mean "
            f"exposure {p['exposure']:.2f} while buy and hold returned {p['buy_and_hold']['total_return']:.1%} "
            f"(log shortfall {shortfall:.2f}); a member that earns in that regime should show marginal value against "
            "the reference (A6).",
            data={"year": year, "log_shortfall": _r(shortfall), "period": p})

    share_gap, top = 0.0, None
    if total > 0 and len(fc) > 1:
        top = max(fc, key=lambda k: (fc[k], k))
        share_gap = fc[top] / total - 1 / len(fc)
    corrs = []
    for f in sleeves:
        v = ((archives.get(f) or {}).get("splits") or {}).get(split_id) or {}
        corrs += [c for o, c in (v.get("correlation") or {}).items() if o in sleeves and c is not None]
    mean_corr = sum(corrs) / len(corrs) if corrs else 0.0
    if max(share_gap, mean_corr) > 0:
        share = f"the {top} sleeve earns {fc[top] / total:.0%} of the reference's dev P&L; " if top else ""
        add("concentration", EXPLORATION, "concentration", max(share_gap, mean_corr),
            "Look for a family outside the current sleeves whose dev returns are not explained by them "
            f"({share}mean residual correlation between the sleeves {mean_corr:.2f}).",
            "All sleeves are trend families; diversification needs a return source with low residual correlation "
            "to them, which the appraisal regression measures directly.",
            data={"family_contribution": fc, "mean_residual_correlation": _r(mean_corr)})

    for f in sleeves:
        v = ((archives.get(f) or {}).get("splits") or {}).get(split_id) or {}
        cost, rep = v.get("cost") or {}, v.get("representative") or {}
        s1, s2 = cost.get("sharpe_1x"), cost.get("sharpe_2x")
        if s1 is not None and s2 is not None and s1 > 0 and s2 < s1:
            add(f"cost:{f}", f, "cost", (s1 - s2) / s1,
                f"Find lower-turnover {f} variants: its representative's dev Sharpe falls from {s1:.2f} to {s2:.2f} "
                "at doubled costs.",
                "Costs scale with turnover; slower signals or a wider deadband keep more of the gross edge.",
                cites=[c for c in (rep.get("experiment_id"), cost.get("experiment_id")) if c],
                data={"sharpe_1x": s1, "sharpe_2x": s2})

    failed = gate2_failed_families(store, split_id)
    for c in conclusions(store):
        fam = c["family_id"]
        if c["product"] != product or fam not in sleeves:
            continue
        for i, q in enumerate(c["body"].get("unresolved") or []):
            if not open_question_allowed(str(q), fam, failed):
                continue
            ids = [e for e in c["body"].get("experiment_ids") or []
                   if (r := store.get_experiment(e)) is not None and r["split_id"] == split_id
                   and r["segment"] == "dev"]
            add(f"open:{c['session_id']}:{i}", fam, "open_question", OPEN_QUESTION_GAP,
                f"Follow up an open question from {c['session_id']}: {str(q)[:600]}",
                f"An earlier {fam} study left this unresolved: {str(q)[:1200]}", cites=ids[:5],
                data={"conclusion": c["id"]})

    for p in cluster_proposals(store, split_id):
        b = p["body"]
        add(f"cluster:{p['id']}", EXPLORATION, "unmapped_cluster", b["size"] / 50,
            f"Investigate the unmapped cluster {b['label']} ({b['size']} trials) as a possible new family.",
            f"No seed family claims these trials; representative {b['representative']['strategy_ref']} has dev "
            f"Sharpe {b['representative']['sharpe']}.",
            cites=[b["representative"]["experiment_id"]], data={"proposal": p["id"]})
    return sorted(out, key=lambda c: (-c["score"], c["key"]))


def batch(paths: Paths, store: ExperimentStore, product: str, n: int = 6) -> list[dict[str, Any]]:
    """The next batch: new keys only, about a third exploration, best scores first."""
    cs = CampaignStore(store)
    live = {r["source"].get("key") for r in cs.list(product) if r["state"] in ("proposed", "queued", "running")}
    pool = [c for c in candidates(paths, store, product) if c["key"] not in live]
    explore = [c for c in pool if c["family"] == EXPLORATION]
    family = [c for c in pool if c["family"] != EXPLORATION]
    n_x = min(len(explore), max(1, round(EXPLORATION_SHARE * n)))
    picked = explore[:n_x] + family[:n - n_x]
    if len(picked) < n:
        picked += [c for c in pool if c not in picked][:n - len(picked)]
    return sorted(picked, key=lambda c: (-c["score"], c["key"]))


def generate(paths: Paths, store: ExperimentStore, product: str, n: int = 6) -> list[dict[str, Any]]:
    """Write the batch as `proposed` campaigns (the human or the auto-replenish loop queues them, A13.11)."""
    cs = CampaignStore(store)
    out = []
    for c in batch(paths, store, product, n):
        source = {"kind": "generator", "diagnostic": c["diagnostic"], "key": c["key"], "gap": c["gap"],
                  "room": c["room"], "score": c["score"], "data": c["data"]}
        row = cs.add(paths, CampaignSpec.model_validate(c["spec"]), source, "controller")
        out.append({"id": row["id"], "family": row["family"], "key": c["key"], "score": c["score"],
                    "goal": row["body"]["goal"]})
    return out
