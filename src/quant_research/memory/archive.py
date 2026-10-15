"""Deterministic family archives (design 4.6): `library/archives/<family>.json` and `.md`, regenerated from the
records after each clustering. Regenerating on the same records gives byte-identical files.

Per split: the family's seed members and representative (highest dev Sharpe among seeds, else among its trials)
with dev statistics and split DSR; parameter ranges tried; trials and clusters; Sharpe at 1x and 2x costs for the
representative; Sharpe by regime; residual correlation with the other families' representatives; failure classes;
holdout viewings used; and the conclusions, oldest first.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quant_research.backtest.engine import CostConfig, EngineConfig
from quant_research.data.snapshot import load_manifest
from quant_research.evaluation.split_stats import dsr_k, sharpe_daily
from quant_research.experiments.gatekeeper import GateContext
from quant_research.experiments.runner import ExperimentRequest, run_experiment
from quant_research.experiments.store import ExperimentStore
from quant_research.library.catalog import Catalog, load_catalog
from quant_research.memory import clusters
from quant_research.memory.conclusions import conclusions
from quant_research.memory.families import map_clusters
from quant_research.memory.residuals import residuals, split_market_returns
from quant_research.memory.returns import ReturnArchive, daily_returns, equity_file
from quant_research.paths import Paths
from quant_research.portfolio.policy import PortfolioPolicy
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.validation.splits import load_split

REGIME_DAYS = 30
ARCHIVE_ONLY = {"short_term_reversal": "Short-term reversal: documented, infeasible at 0.6% per side; archive only."}


def _r(x: Any, nd: int = 4) -> Any:
    return None if x is None or (isinstance(x, float) and not np.isfinite(x)) else round(float(x), nd)


def split_product(paths: Paths, split_id: str) -> str:
    return load_manifest(paths, load_split(paths, split_id).dataset_id)["instrument"]


def _exp(store: ExperimentStore, exp_id: str) -> dict[str, Any]:
    return store.get_experiment(exp_id) or {}


def _cost_x2(paths: Paths, store: ExperimentStore, exp: dict[str, Any], sandbox: SandboxConfig) -> dict[str, Any]:
    """Sharpe of the representative with doubled fee and slippage, from a `sensitivity` rerun (not a trial)."""
    if exp["strategy_ref"].startswith("plugin:") and sandbox.isolation != "docker":
        return {"sharpe_2x": None, "note": "plugin: rerun only under docker isolation"}
    eng = EngineConfig.model_validate(exp["engine_config"])
    eng2 = eng.model_copy(update={"costs": CostConfig(fee_rate=eng.costs.fee_rate * 2,
                                                      slippage_bps=eng.costs.slippage_bps * 2,
                                                      source=f"2x of: {eng.costs.source}")})
    prior = store.query("SELECT id, result_dir FROM experiments WHERE kind='sensitivity' AND status='completed' "
                        "AND candidate_hash=? AND split_id=? AND segment='dev' AND engine_config_id=? "
                        "ORDER BY created_at LIMIT 1",
                        (exp["candidate_hash"], exp["split_id"], eng2.config_id()))
    if prior:
        rid, rdir = prior[0]["id"], prior[0]["result_dir"]
    else:
        req = ExperimentRequest(strategy_ref=exp["strategy_ref"], parameters=exp["parameters"] or {},
                                dataset_id=exp["dataset_id"], split_id=exp["split_id"], segment="dev",
                                engine_config=eng2, policy=PortfolioPolicy.model_validate(exp["policy"] or {}))
        res = run_experiment(paths, store, req, kind="sensitivity", parent_id=exp["id"], sandbox=sandbox,
                             gate=GateContext(caller="controller", reuse_duplicates=False))
        if res.status != "completed":
            return {"sharpe_2x": None, "note": f"rerun {res.status}"}
        rid, rdir = res.experiment_id, res.result_dir
    f = equity_file(paths, {"result_dir": rdir})
    if f is None:
        return {"sharpe_2x": None, "note": f"{rid}: equity file missing"}
    r = daily_returns(pd.read_parquet(f, columns=["time", "equity"]), eng.initial_cash)
    return {"sharpe_2x": _r(sharpe_daily(r.to_numpy())), "experiment_id": rid}


def _regimes(r: pd.Series, market: pd.Series) -> dict[str, Any]:
    trail = (1 + market).rolling(REGIME_DAYS).apply(np.prod, raw=True) - 1
    vol = market.rolling(REGIME_DAYS).std()
    ok = trail.notna()
    high = vol > vol[ok].median()
    masks = {"trend_up": ok & (trail > 0), "trend_down": ok & (trail <= 0), "high_vol": ok & high,
             "low_vol": ok & ~high}
    return {k: {"days": int(m.sum()), "sharpe": _r(sharpe_daily(r[m].to_numpy()))} for k, m in masks.items()}


def _param_ranges(exps: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for ref, group in sorted(_group(exps, "strategy_ref").items()):
        ranges: dict[str, list[float]] = {}
        for e in group:
            for k, v in (e["parameters"] or {}).items():
                if isinstance(v, int | float) and not isinstance(v, bool):
                    lo, hi = ranges.get(k, [v, v])
                    ranges[k] = [min(lo, v), max(hi, v)]
        out[ref] = {"trials": len(group), "ranges": dict(sorted(ranges.items()))}
    return out


def _group(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


def _split_view(paths: Paths, store: ExperimentStore, split_id: str, catalog: Catalog,
                sandbox: SandboxConfig, families: list[str]) -> dict[str, dict[str, Any]]:
    last = clusters.latest(store, split_id)
    if last is None:
        return {}
    returns = ReturnArchive(paths, store).load(split_id)
    market = split_market_returns(paths, split_id)
    resid, _ = residuals(returns, market)
    k07 = int(last["k"]["0.7"])
    lab03 = clusters.members(store, last["id"], 0.3)
    lab07 = clusters.members(store, last["id"], 0.7)
    mapping = map_clusters(store, last["id"])
    index = {r["experiment_id"]: r for r in ReturnArchive(paths, store).index(split_id)}
    seed_of = {r["id"]: json.loads(r["notes"]) for r in store.query(
        "SELECT id, notes FROM experiments WHERE kind='seed' AND split_id=? AND status='completed'", (split_id,))}
    entry_family = {e.id: e.family for e in catalog.entries}

    def stats(exp_id: str) -> dict[str, Any]:
        e, r = _exp(store, exp_id), returns[exp_id].to_numpy()
        return {"experiment_id": exp_id, "strategy_ref": e.get("strategy_ref"), "parameters": e.get("parameters"),
                "sharpe": _r(sharpe_daily(r)), "max_drawdown": _r((e.get("metrics") or {}).get("max_drawdown")),
                "turnover": _r((e.get("metrics") or {}).get("turnover")), "split_dsr": _r(dsr_k(r, k07)),
                "cluster_0.3": lab03.get(exp_id), "cluster_0.7": lab07.get(exp_id)}

    views: dict[str, dict[str, Any]] = {}
    for fam in families:
        mine = {lb: m for lb, m in mapping.items() if fam in m["families"]}
        trials = sorted(e for m in mine.values() for e in m["members"])
        seeds = sorted(i for i, n in seed_of.items() if entry_family.get(n.get("library_entry")) == fam
                       and i in returns.columns)
        pool = seeds or [t for t in trials if t in returns.columns]
        rep = max(pool, key=lambda i: (sharpe_daily(returns[i].to_numpy()), i)) if pool else None
        exps = [_exp(store, t) for t in trials if index.get(t, {}).get("kind") != "seed"]
        labels = Counter(lb["label"] for t in trials for lb in ((_exp(store, t).get("diagnosis") or {})
                                                               .get("labels", [])))
        views[fam] = {
            "k": last["k"], "clustering_input": last["input_hash"],
            "members": [stats(s) | {"instance": seed_of[s]["instance"]} for s in seeds],
            "representative": stats(rep) if rep else None,
            "trials": len(trials), "trials_in_shared_clusters": sum(m["size"] for m in mine.values() if m["shared"]),
            "clusters_0.3": [{"label": lb, "size": m["size"], "is_shared": m["shared"],
                              "shared_with": [f for f in m["families"] if f != fam]} for lb, m in sorted(mine.items())],
            "clusters_0.7": len({lab07[t] for t in trials if t in lab07}),
            "parameters_tried": _param_ranges(exps),
            "cost": ({"sharpe_1x": _r(sharpe_daily(returns[rep].to_numpy()))}
                     | _cost_x2(paths, store, _exp(store, rep), sandbox)) if rep else None,
            "regimes": _regimes(returns[rep], market) if rep else None,
            "failure_classes": dict(sorted(labels.items())),
            "no_returns": sum(1 for t in trials if index.get(t, {}).get("no_returns")),
            "holdout_viewings": len(store.holdout_runs(split_id)),
            "_rep": rep,
        }
    reps = {f: v["_rep"] for f, v in views.items() if v["_rep"]}
    for fam, v in views.items():
        rep = v.pop("_rep")
        v["correlation"] = {o: _r(float(np.corrcoef(resid[rep], resid[r])[0, 1]), 3)
                            for o, r in sorted(reps.items()) if rep and o != fam}
    return views


def _families_of_conclusion(c: dict[str, Any], trial_families: dict[str, list[str]]) -> list[str]:
    """Its own family, else the families with the most cited experiments (all of them on a tie, A13.2)."""
    if c["family_id"]:
        return [c["family_id"]]
    votes = Counter(f for e in (c["body"].get("experiment_ids") or []) for f in trial_families.get(e, []))
    top = max(votes.values(), default=0)
    return sorted(f for f, n in votes.items() if n == top) if top else []


def build_archives(paths: Paths, store: ExperimentStore, catalog: Catalog | None = None,
                   sandbox: SandboxConfig | None = None) -> dict[str, dict[str, Any]]:
    catalog = catalog or load_catalog()
    sandbox = sandbox or SandboxConfig(isolation="subprocess")
    fams = {f.id: {"name": f.name, "thesis": f.thesis, "sleeve": f.sleeve} for f in catalog.families}
    for fid, thesis in ARCHIVE_ONLY.items():
        fams.setdefault(fid, {"name": fid.replace("_", " ").capitalize(), "thesis": thesis, "sleeve": False})
    families = sorted(fams)
    by_split = {s: _split_view(paths, store, s, catalog, sandbox, families)
                for s in ReturnArchive(paths, store).splits()}
    trial_families: dict[str, list[str]] = {}
    for s in by_split:
        last = clusters.latest(store, s)
        if last:
            for m in map_clusters(store, last["id"]).values():
                for e in m["members"]:
                    trial_families[e] = m["families"]
    concl: dict[str, list[dict[str, Any]]] = {f: [] for f in families}
    for c in conclusions(store):
        fams_c = [f for f in _families_of_conclusion(c, trial_families) if f in concl]
        for fam in fams_c:
            concl[fam].append({"session_id": c["session_id"], "product": c["product"], "topic": c["body"]["topic"],
                               "decision": c["body"]["decision"], "hypothesis": c["body"]["hypothesis"],
                               "unresolved": c["body"]["unresolved"], "citations_ok": bool(c["citations_ok"]),
                               "shared_with": [f for f in fams_c if f != fam]})
    out = {}
    for f in families:
        out[f] = {"family": f, **fams[f],
                  "splits": {s: {"product": split_product(paths, s), **v[f]} for s, v in sorted(by_split.items())
                             if f in v},
                  "conclusions": concl[f]}
    return out


def render_md(a: dict[str, Any]) -> str:
    lines = [f"# {a['name']} (`{a['family']}`)", "", a["thesis"], "",
             f"Sleeve: {'yes' if a['sleeve'] else 'no'}. Conclusions: {len(a['conclusions'])}.", ""]
    for split_id, v in a["splits"].items():
        k = v["k"]
        lines += [f"## {v['product']} (`{split_id}`)", "",
                  f"Split K (ρ 0.3 / 0.5 / 0.7): {k['0.3']} / {k['0.5']} / {k['0.7']}. Trials mapped to the family: "
                  f"{v['trials']} in {len(v['clusters_0.3'])} clusters at 0.3 ({v['clusters_0.7']} at 0.7). "
                  f"Holdout viewings used: {v['holdout_viewings']}.", ""]
        shared = [c for c in v["clusters_0.3"] if c["is_shared"]]
        if shared:
            lines += ["Shared clusters (seed families tie, appendix A13.2; also in the archives of "
                      + ", ".join(sorted({f for c in shared for f in c["shared_with"]})) + "): "
                      + ", ".join(f"`{c['label']}` ({c['size']} trials)" for c in shared)
                      + f"; {v['trials_in_shared_clusters']} of the {v['trials']} trials.", ""]
        if v["members"]:
            lines += ["| member | dev Sharpe | max DD | turnover | split DSR |", "|---|---|---|---|---|"]
            lines += [f"| {m['instance']} | {m['sharpe']} | {m['max_drawdown']} | {m['turnover']} | {m['split_dsr']} |"
                      for m in v["members"]]
            lines.append("")
        rep = v["representative"]
        if rep:
            cost = v["cost"] or {}
            lines += [f"Representative: `{rep['experiment_id']}` ({rep['strategy_ref']}), dev Sharpe {rep['sharpe']}, "
                      f"split DSR {rep['split_dsr']}; Sharpe at 2× costs {cost.get('sharpe_2x')}"
                      + (f" ({cost['note']})" if cost.get("note") else "") + ".",
                      "Regimes: " + ", ".join(f"{name} {r['sharpe']} ({r['days']} days)"
                                              for name, r in v["regimes"].items()) + ".",
                      "Residual correlation with other families: "
                      + (", ".join(f"{o} {c}" for o, c in v["correlation"].items()) or "none") + ".", ""]
        if v["parameters_tried"]:
            lines.append("Parameters tried: " + "; ".join(
                f"{ref} ×{p['trials']} {json.dumps(p['ranges'])}" for ref, p in v["parameters_tried"].items()) + ".")
        if v["failure_classes"]:
            lines.append("Diagnosis labels: " + ", ".join(f"{k} {n}" for k, n in v["failure_classes"].items()) + ".")
        lines.append("")
    if a["conclusions"]:
        lines += ["## Conclusions", ""]
        for c in a["conclusions"]:
            flag = "" if c["citations_ok"] else " (citation check failed)"
            lines.append(f"- `{c['session_id']}` {c['product']} {c['topic']}: {c['decision']}{flag}. {c['hypothesis']}")
    return "\n".join(lines).rstrip() + "\n"


def write_archives(paths: Paths, store: ExperimentStore, catalog: Catalog | None = None,
                   sandbox: SandboxConfig | None = None) -> list[Path]:
    d = paths.library / "archives"
    d.mkdir(parents=True, exist_ok=True)
    written = []
    for fam, a in build_archives(paths, store, catalog, sandbox).items():
        for suffix, text in ((".json", json.dumps(a, indent=2, sort_keys=True, ensure_ascii=False) + "\n"),
                             (".md", render_md(a))):
            p = d / f"{fam}{suffix}"
            tmp = p.with_suffix(suffix + ".tmp")
            tmp.write_text(text)
            os.replace(tmp, p)
            written.append(p)
    return written
