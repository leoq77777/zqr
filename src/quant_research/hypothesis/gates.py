"""Gate 1 (marginal value on dev) and gate 2 (do no harm on validation) for a registered hypothesis (appendix A6,
A13.6, A13.7). The controller runs them; agents get the verdicts only (`verdict`)."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from quant_research.evaluation.split_stats import appraisal_test, sharpe_daily
from quant_research.experiments.gatekeeper import GateContext
from quant_research.experiments.runner import ExperimentRequest, run_experiment
from quant_research.experiments.store import ExperimentStore
from quant_research.hypothesis.store import HypothesisError, HypothesisStore
from quant_research.library.store import LibraryStore
from quant_research.memory.returns import daily_returns, research_equity_file
from quant_research.paths import Paths
from quant_research.portfolio.evaluate import _summary, gate1_reference, run_segment
from quant_research.portfolio.version import Member, PortfolioVersion, VersionStore
from quant_research.research.mandate import Mandate
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.timeutil import parse_time

GATE2 = {"max_drawdown_slack": 0.02, "max_turnover_ratio": 1.25, "sharpe_slack": 0.05}
PERIOD_CUTS = ("2021-01-01",)  # the long-dev review's periods (A13.7 diagnostic)
MIN_DAYS = 30


def _controller_gate() -> GateContext:
    return GateContext(caller="controller", reuse_duplicates=False, allow_weak_isolation=False)


def _reference_dev(store: ExperimentStore, vid: str) -> dict[str, Any]:
    for e in reversed(VersionStore(store).events(vid)):
        ev = json.loads(e["evidence"]) if e["evidence"] else {}
        if ev.get("gate1_reference"):
            return store.get_experiment(ev["dev_experiment"])
    raise HypothesisError(f"{vid} is not recorded as a gate 1 reference")


def _returns(paths: Paths, row: dict[str, Any]) -> pd.Series:
    """Daily returns of a recorded run; a portfolio run's without the ladder (A13.6)."""
    path = research_equity_file(paths, row)
    if path is None:
        raise HypothesisError(f"equity of {row['id']} not found")
    return daily_returns(pd.read_parquet(path), row["engine_config"]["initial_cash"])


def _aligned(c: pd.Series, p: pd.Series) -> pd.DataFrame:
    both = pd.concat({"c": c, "p": p}, axis=1, join="inner").dropna()
    if len(both) < MIN_DAYS:
        raise HypothesisError(f"only {len(both)} common days between the variant and the reference")
    return both


def hedged_periods(both: pd.DataFrame, beta: float, cuts=PERIOD_CUTS) -> list[dict[str, Any]]:
    """The hedged series' Sharpe per period and over the whole span: a diagnostic, never part of the verdict."""
    h = both["c"] - beta * both["p"]
    edges = [pd.Timestamp(parse_time(c), unit="s").tz_localize(None) for c in cuts]
    edges = [e for e in edges if h.index[0] < e <= h.index[-1]]
    bounds = [h.index[0], *edges, h.index[-1] + pd.Timedelta(days=1)]
    out = []
    for a, b in zip(bounds, bounds[1:], strict=False):
        part = h[(h.index >= a) & (h.index < b)]
        out.append({"from": str(part.index[0].date()), "to": str(part.index[-1].date()), "days": len(part),
                    "hedged_sharpe": round(sharpe_daily(part.to_numpy()), 4)})
    if len(out) > 1:
        out.append({"from": str(h.index[0].date()), "to": str(h.index[-1].date()), "days": len(h),
                    "hedged_sharpe": round(sharpe_daily(h.to_numpy()), 4)})
    return out


def _regression(both: pd.DataFrame, k: int) -> dict[str, Any]:
    test = appraisal_test(both["c"].to_numpy(), both["p"].to_numpy(), k)
    return {**{key: (round(v, 6) if isinstance(v, float) else v) for key, v in test.items()}, "days": len(both),
            "periods": hedged_periods(both, test["beta"])}


# -- gate 1 ------------------------------------------------------------------------------------------------

def variant_run(paths: Paths, store: ExperimentStore, hid: str, index: int,
                sandbox: SandboxConfig | None = None) -> dict[str, Any]:
    """The variant alone on the full dev of the hypothesis split, with the reference's engine configuration: a
    trial of kind `registered`. A run already recorded for this variant is reused."""
    hs = HypothesisStore(store)
    row = hs.get(hid)
    found = store.query("SELECT id FROM experiments WHERE kind='registered' AND json_extract(notes, '$.hypothesis')=? "
                        "AND json_extract(notes, '$.variant')=? AND status NOT IN ('blocked','running') "
                        "ORDER BY created_at DESC LIMIT 1", (hid, index))
    if found:
        return store.get_experiment(found[0]["id"])
    variant = hs.hypothesis(hid).variants[index]
    mandate = Mandate.model_validate(store.get_mandate(row["mandate_id"])["body"])
    reference = VersionStore(store).version(row["reference_id"])
    req = ExperimentRequest(strategy_ref=variant.strategy_ref, parameters=variant.parameters,
                            dataset_id=reference.dataset_id, split_id=row["split_id"], segment="dev",
                            engine_config=reference.engine_config, policy=mandate.policy)
    res = run_experiment(paths, store, req, kind="registered", sandbox=sandbox, gate=_controller_gate(),
                         record={"notes": json.dumps({"hypothesis": hid, "variant": index})})
    if res.status == "blocked":
        raise HypothesisError(f"variant {index + 1} was blocked: {res.failure_reason}")
    return store.get_experiment(res.experiment_id)


def gate1(paths: Paths, store: ExperimentStore, hid: str, sandbox: SandboxConfig | None = None) -> dict[str, Any]:
    """A6 regression of each variant on the reference's dev run without the ladder; K = number of variants.
    Recorded once; a second call returns the recorded result."""
    hs = HypothesisStore(store)
    row = hs.get(hid)
    if row is None:
        raise HypothesisError(f"unknown hypothesis {hid}")
    if (done := hs.event(hid, "gate1")) is not None:
        return done["evidence"]
    ref_row = _reference_dev(store, row["reference_id"])
    rp = _returns(paths, ref_row)
    variants = hs.hypothesis(hid).variants
    results = []
    for i in range(len(variants)):
        exp = variant_run(paths, store, hid, i, sandbox)
        out: dict[str, Any] = {"variant": i, "experiment_id": exp["id"], "status": exp["status"]}
        if exp["status"] not in ("completed", "duplicate"):
            results.append(out | {"passed": False, "reason": exp["failure_reason"]})
            continue
        results.append(out | _regression(_aligned(_returns(paths, exp), rp), len(variants)))
    passing = [r for r in results if r["passed"]]
    best = max(passing, key=lambda r: r["appraisal_ratio"])["variant"] if passing else None
    evidence = {"passed": best is not None, "best_variant": best, "k": len(variants),
                "reference_id": row["reference_id"], "reference_experiment": ref_row["id"], "variants": results}
    hs.record(hid, "gate1", "controller", evidence)
    return evidence


def marginal(paths: Paths, store: ExperimentStore, experiment_id: str) -> dict[str, Any]:
    """The gate 1 regression for any completed dev experiment against its split's reference, without a verdict
    (dev data only)."""
    exp = store.get_experiment(experiment_id)
    if exp is None or exp["segment"] != "dev" or exp["status"] not in ("completed", "duplicate"):
        raise HypothesisError(f"{experiment_id} is not a completed dev experiment")
    products = sorted({v["product"] for v in VersionStore(store).list(split_id=exp["split_id"])})
    refs = [r for r in (gate1_reference(store, p, exp["split_id"]) for p in products) if r]
    if not refs:
        raise HypothesisError(f"no gate 1 reference on {exp['split_id']}")
    ref_row = _reference_dev(store, refs[0])
    reg = _regression(_aligned(_returns(paths, exp), _returns(paths, ref_row)), 1)
    return {"experiment_id": experiment_id, "reference_id": refs[0], "reference_experiment": ref_row["id"],
            **{k: reg[k] for k in ("beta", "alpha_daily", "appraisal_ratio", "days", "periods")}}


# -- gate 2 ------------------------------------------------------------------------------------------------

def candidate_version(paths: Paths, store: ExperimentStore, hid: str, index: int) -> PortfolioVersion:
    """The reference plus the variant as a member at full weight (a new library entry, status candidate)."""
    hs = HypothesisStore(store)
    row = hs.get(hid)
    variant = hs.hypothesis(hid).variants[index]
    lib = LibraryStore(paths.db)
    try:
        eid = lib.add_candidate(row["product"], row["family"], variant.strategy_ref, variant.parameters,
                                source=f"hypothesis:{hid}")
    finally:
        lib.close()
    ref = VersionStore(store).version(row["reference_id"])
    body = ref.model_dump(mode="json")
    member = Member(entry_id=eid, family=row["family"], strategy_ref=variant.strategy_ref,
                    parameters=variant.parameters)
    body |= {"members": [*body["members"], member.model_dump(mode="json")], "parent_id": row["reference_id"],
             "notes": f"hypothesis {hid}, variant {index + 1}"}
    return PortfolioVersion.model_validate(body)


def _measures(summary: dict[str, Any]) -> dict[str, Any]:
    m = {"sharpe": summary.get("sharpe_daily_no_ladder"), "max_drawdown": summary.get("max_drawdown_no_ladder"),
         "turnover": summary.get("turnover_no_ladder")}
    if any(v is None for v in m.values()):
        raise HypothesisError(f"{summary['experiment_id']} has no measures without the ladder (recorded before "
                              "release 4c); gate 2 cannot compare it")
    return m


def gate2_checks(ref: dict[str, float], cand: dict[str, float]) -> dict[str, bool]:
    return {
        "sharpe_not_lower": cand["sharpe"] >= ref["sharpe"] - GATE2["sharpe_slack"],
        "drawdown_within_2_points": cand["max_drawdown"] >= ref["max_drawdown"] - GATE2["max_drawdown_slack"],
        "turnover_within_25_percent": cand["turnover"] <= ref["turnover"] * GATE2["max_turnover_ratio"],
    }


def gate2(paths: Paths, store: ExperimentStore, hid: str, sandbox: SandboxConfig | None = None) -> dict[str, Any]:
    """Both portfolios on validation without the ladder (A13.6); pass if Sharpe is at most `sharpe_slack` lower,
    the drawdown at most 2 points worse and turnover at most 25% higher (A13.9). Runs once per hypothesis."""
    hs = HypothesisStore(store)
    row = hs.get(hid)
    if row is None:
        raise HypothesisError(f"unknown hypothesis {hid}")
    if hs.event(hid, "gate2") is not None:
        raise HypothesisError(f"gate 2 already ran for {hid}")
    g1 = hs.event(hid, "gate1")
    if g1 is None or not g1["evidence"]["passed"]:
        raise HypothesisError(f"{hid} has no variant that passed gate 1")
    vs = VersionStore(store)
    cid = vs.put(candidate_version(paths, store, hid, g1["evidence"]["best_variant"]))
    ref = _measures(_summary(run_segment(paths, store, row["reference_id"], "validation", sandbox)))
    cand = _measures(_summary(run_segment(paths, store, cid, "validation", sandbox)))
    checks = gate2_checks(ref, cand)
    passed = all(checks.values())
    evidence = {"passed": passed, "version_id": cid, "reference_id": row["reference_id"], "checks": checks,
                "reference": ref, "candidate": cand, "rule": GATE2, "hypothesis": hid}
    vs.transition(cid, "admitted" if passed else "failed_validation", "controller", {"gate2": evidence},
                  note=f"gate 2 of {hid}")
    hs.record(hid, "gate2", "controller", evidence)
    return evidence


def run_gates(paths: Paths, store: ExperimentStore, hid: str,
              sandbox: SandboxConfig | None = None) -> dict[str, Any]:
    """Gate 1, then gate 2 if a variant passed and gate 2 has not run."""
    hs = HypothesisStore(store)
    g1 = gate1(paths, store, hid, sandbox)
    g2 = hs.event(hid, "gate2")
    if g1["passed"] and g2 is None:
        gate2(paths, store, hid, sandbox)
        g2 = hs.event(hid, "gate2")
    return {"hypothesis_id": hid, "state": hs.state(hid), "gate1": g1, "gate2": g2["evidence"] if g2 else None}


def report_lines(store: ExperimentStore, next_split: str) -> list[str]:
    """Daily report: every registered hypothesis with its verdicts; admitted versions wait for the next split."""
    hs = HypothesisStore(store)
    rows = hs.list()
    if not rows:
        return []
    out = ["", "| hypothesis | product | family | variants | state | gate 1 best AR | version |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        g1 = (hs.event(r["id"], "gate1") or {}).get("evidence") or {}
        g2 = (hs.event(r["id"], "gate2") or {}).get("evidence") or {}
        ars = [v["appraisal_ratio"] for v in g1.get("variants", []) if "appraisal_ratio" in v]
        out.append(f"| {r['id']} | {r['product']} | {r['family']} | {len(r['body']['variants'])} | {r['state']} | "
                   f"{f'{max(ars):.3f}' if ars else '-'} | {g2.get('version_id', '-')} |")
    admitted = [r for r in rows if r["state"] == "admitted"]
    if admitted:
        out.append(f"- Admitted versions ({len(admitted)}) wait for the next split's holdout, earliest {next_split} "
                   "(design D5); they are not frozen or paper-approved on this split. They run as named shadows.")
    return out


def verdict(store: ExperimentStore, hid: str) -> dict[str, Any]:
    """What an agent may see (A6): the state and which gate failed, never validation measures."""
    hs = HypothesisStore(store)
    state = hs.state(hid)
    return {"hypothesis_id": hid, "state": state,
            "failed_gate": {"gate1_failed": 1, "gate2_failed": 2}.get(state)}

