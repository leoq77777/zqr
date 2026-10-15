"""Clustering report for the phase 2 checkpoint: `library/reports/clustering-<YYYY-MM-DD>.md`."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quant_research.evaluation.split_stats import dsr_k, sharpe_daily, sigma0, sr0
from quant_research.experiments.store import ExperimentStore
from quant_research.memory import clusters
from quant_research.memory.archive import split_product
from quant_research.memory.characterise import seed_runs
from quant_research.memory.conclusions import conclusions
from quant_research.memory.families import map_clusters, proposals
from quant_research.memory.returns import ReturnArchive
from quant_research.paths import Paths

# 2026-10-13 feasibility run (design 4.3), first principal component as the market stand-in: raw (winsorised).
FEASIBILITY = {"BTC-USD": {"0.3": "34 (43)", "0.5": "113 (116)", "0.7": "255 (267)"},
               "ETH-USD": {"0.3": "34 (38)", "0.5": "108 (110)", "0.7": "265 (276)"}}


Z90 = 1.2815515655446004


def _size_bands(sizes: list[int]) -> str:
    bands = Counter("1" if s == 1 else "2-5" if s <= 5 else "6-20" if s <= 20 else ">20" for s in sizes)
    return ", ".join(f"{b}: {bands.get(b, 0)}" for b in ("1", "2-5", "6-20", ">20"))


def _f(x: Any, nd: int = 2) -> str:
    return "–" if x is None else f"{x:.{nd}f}"


def split_section(paths: Paths, store: ExperimentStore, split_id: str) -> list[str]:
    last = clusters.latest(store, split_id)
    if last is None:
        return [f"## {split_id}", "", "No clustering yet (`zqr memory cluster`).", ""]
    product = split_product(paths, split_id)
    raw = clusters.compute(paths, store, split_id, winsorise=False)
    k, counts = last["k"], last["summary"]
    returns = ReturnArchive(paths, store).load(split_id)
    k07 = int(k["0.7"])
    index = {r["experiment_id"]: r for r in ReturnArchive(paths, store).index(split_id)}
    lab = {t: clusters.members(store, last["id"], t) for t in clusters.THRESHOLDS}
    sizes = {t: Counter(v for e, v in lab[t].items() if not index.get(e, {}).get("no_returns")
                        and v not in (clusters.FLAT, clusters.PASSIVE)) for t in clusters.THRESHOLDS}
    mapping = map_clusters(store, last["id"])
    feas = FEASIBILITY.get(product, {})
    lines = [f"## {product} (`{split_id}`)", "",
             f"Trials: {counts['trials']} ({counts['with_returns']} with returns, {counts['no_returns']} without; "
             f"{counts['flat']} flat, {counts['passive']} passive). Clustering input `{last['input_hash']}`.", "",
             "| ρ threshold | 0.3 | 0.5 | 0.7 |", "|---|---|---|---|",
             f"| K (winsorised, used) | {k['0.3']} | {k['0.5']} | **{k['0.7']}** |",
             f"| K (not winsorised) | {raw.k['0.3']} | {raw.k['0.5']} | {raw.k['0.7']} |",
             f"| 2026-10-13 feasibility run, raw (winsorised) | {feas.get('0.3', '–')} | {feas.get('0.5', '–')} | "
             f"{feas.get('0.7', '–')} |", "",
             f"K counts every cluster once, plus one for the flat cluster, one for the passive cluster and one per "
             f"trial without returns ({counts['no_returns']}). With K = {k07} on T = {len(returns)} days: "
             f"SR0(K) = {_f(sr0(k07, len(returns)))}, σ0 = {_f(sigma0(len(returns)))}, so a split DSR of 0.9 needs "
             f"an annualised dev Sharpe of at least {_f(sr0(k07, len(returns)) + Z90 * sigma0(len(returns)))} "
             f"(more if fat tails widen σ_SR).", "",
             "Cluster sizes (trials with returns, excluding flat and passive):", ""]
    for t in clusters.THRESHOLDS:
        s = sizes[t]
        lines.append(f"- ρ {t}: {len(s)} clusters; sizes {_size_bands(list(s.values()))}; largest "
                     f"{max(s.values()) if s else 0}")
    lines += ["", "Largest clusters at ρ 0.3:", "",
              "| cluster | size | family (basis) | session topics | strategies | best dev Sharpe |",
              "|---|---|---|---|---|---|"]
    for label, m in sorted(mapping.items(), key=lambda kv: (-kv[1]["size"], kv[0]))[:15]:
        topics = Counter((index[e]["tag_source"] or "untagged").replace("topic:", "").split(":")[0]
                         if not (index[e]["tag_source"] or "").startswith("seed:") else "seed" for e in m["members"])
        refs = Counter(r["strategy_ref"] if r["strategy_ref"].startswith("template:") else "plugin"
                       for r in store.query(f"SELECT strategy_ref FROM experiments WHERE id IN "
                                            f"({','.join('?' * len(m['members']))})", m["members"]))
        best = max((index[e]["sharpe"] for e in m["members"] if index[e]["sharpe"] is not None), default=None)
        fam = f"{m['family']} ({m['basis']})" if m["family"] else "unmapped"
        lines.append(f"| `{label}` | {m['size']} | {fam} | "
                     + ", ".join(f"{t} {n}" for t, n in topics.most_common(4)) + " | "
                     + ", ".join(f"{r} {n}" for r, n in refs.most_common(3)) + f" | {_f(best)} |")
    mapped = Counter(m["family"] or "unmapped" for m in mapping.values() for _ in m["members"])
    lines += ["", "Trials by family at ρ 0.3: " + ", ".join(f"{f} {n}" for f, n in sorted(mapped.items())) + ".", ""]
    seeds = seed_runs(store, split_id)
    if seeds:
        lines += ["Seed placement:", "",
                  "| seed | dev Sharpe | split DSR | cluster ρ 0.3 (size) | cluster ρ 0.7 (size) |",
                  "|---|---|---|---|---|"]
        for inst, exp_id in sorted(seeds.items()):
            if index.get(exp_id, {}).get("flat"):
                lines.append(f"| {inst} | flat | – | `flat` | `flat` |")
                continue
            if exp_id not in returns.columns:
                lines.append(f"| {inst} | – | – | {lab[0.3].get(exp_id, '–')} | {lab[0.7].get(exp_id, '–')} |")
                continue
            r = returns[exp_id].to_numpy()
            c3, c7 = lab[0.3].get(exp_id), lab[0.7].get(exp_id)
            lines.append(f"| {inst} | {_f(sharpe_daily(r))} | {_f(dsr_k(r, k07), 3)} | "
                         f"`{c3}` ({sizes[0.3].get(c3, 1)}) | `{c7}` ({sizes[0.7].get(c7, 1)}) |")
        lines.append("")
    props = proposals(store, split_id)
    lines += [f"New-family proposals ({len(props)}; clusters of at least 5 trials that no seed or family majority "
              "claims):", ""]
    for p in props:
        b = p["body"]
        rep = b["representative"]
        lines.append(f"- `{p['id']}`: {b['size']} trials; topics {json.dumps(b['topics'])}; median dev Sharpe "
                     f"{_f(b['median_sharpe'])}; representative `{rep['experiment_id']}` ({rep['strategy_ref']}, "
                     f"Sharpe {_f(rep['sharpe'])})")
    if not props:
        lines.append("- none")
    top = sorted((r for r in index.values() if r["sharpe"] is not None and not r["flat"]),
                 key=lambda r: -r["sharpe"])[:10]
    lines += ["", "Top 10 non-flat trials by dev Sharpe (daily), split DSR against the legacy DSR:", "",
              f"| experiment | kind | source | dev Sharpe | split DSR (K = {k07}) | legacy DSR (hourly) |",
              "|---|---|---|---|---|---|"]
    for r in top:
        legacy = (store.get_experiment(r["experiment_id"])["statistics"] or {}).get("dsr")
        lines.append(f"| `{r['experiment_id']}` | {r['kind']} | {r['tag_source'] or '–'} | {_f(r['sharpe'])} | "
                     f"{_f(dsr_k(returns[r['experiment_id']].to_numpy(), k07), 3)} | {_f(legacy, 3)} |")
    return lines + [""]


def write_report(paths: Paths, store: ExperimentStore, day: str | None = None) -> Path:
    day = day or datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [f"# Clustering report {day}", "",
             "Rules: `docs/contracts-v2-appendix-a.md` (A1-A5, A7, A12). Residual correlations after removing the "
             "coin's daily return; average linkage; K(0.7) is the effective number of trials used by the split "
             "DSR.", ""]
    for split_id in ReturnArchive(paths, store).splits():
        lines += split_section(paths, store, split_id)
    rows = conclusions(store)
    if rows:
        table = Counter((c["body"].get("topic") or "unknown", c["body"].get("decision") or "none") for c in rows)
        lines += ["## Session conclusions", "",
                  f"{len(rows)} imported; citation check failed for {sum(not c['citations_ok'] for c in rows)}.", "",
                  "| topic | decision | sessions |", "|---|---|---|"]
        lines += [f"| {t} | {d} | {n} |" for (t, d), n in sorted(table.items())]
    out = paths.library / "reports" / f"clustering-{day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines).rstrip() + "\n")
    return out
