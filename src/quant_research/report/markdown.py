"""Replayable Markdown reports with equity/drawdown charts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from quant_research.data.snapshot import load_manifest  # noqa: E402
from quant_research.experiments.store import ExperimentStore  # noqa: E402
from quant_research.paths import Paths  # noqa: E402
from quant_research.timeutil import to_iso  # noqa: E402

PCT_KEYS = {"total_return", "cagr", "ann_volatility", "max_drawdown", "win_rate", "exposure"}


def _fmt(key: str, value: Any) -> str:
    if value is None:
        return "-"
    if key in PCT_KEYS:
        return f"{value * 100:.2f}%"
    if isinstance(value, float):
        return f"{value:,.4f}"
    return str(value)


def _chart(equity: pd.DataFrame, initial_cash: float, out: Path, title: str) -> None:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    eq = equity["equity"]
    bh = equity["close"] / equity["close"].iloc[0] * initial_cash
    ax1.plot(equity["time"], eq, label="strategy", lw=1.2)
    ax1.plot(equity["time"], bh, label="buy & hold (no costs)", lw=0.9, alpha=0.7)
    ax1.set_title(title)
    ax1.legend()
    ax1.grid(alpha=0.3)
    dd = eq / eq.cummax() - 1
    ax2.fill_between(equity["time"], dd, 0, color="tab:red", alpha=0.4)
    ax2.set_ylabel("drawdown")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)


def _statistics_section(s: dict[str, Any] | None) -> list[str]:
    if not s:
        return []
    lines = ["", "## Statistical validation", "",
             f"- Probabilistic Sharpe vs 0 (PSR): {s['psr_0']:.3f} over {s['n_bars']} bars "
             f"(skew {s['skewness']:.2f}, kurtosis {s['kurtosis']:.2f})"]
    if "dsr" in s:
        lines.append(f"- Deflated Sharpe (DSR): **{s['dsr']:.3f}** given {s['n_trials']} research trial(s) on the "
                     f"split at the time of this run; expected best-of-trials Sharpe under no skill "
                     f"{s['expected_max_sharpe_annual']:.2f} (variance: {s['trial_sharpe_variance_source']})")
    if "sharpe_ci_05" in s:
        lines.append(f"- Block bootstrap ({s['bootstrap_samples']} samples, block {s['bootstrap_block']} bars): "
                     f"Sharpe 90% CI [{s['sharpe_ci_05']:.2f}, {s['sharpe_ci_95']:.2f}], total return 90% CI "
                     f"[{s['total_return_ci_05']:.1%}, {s['total_return_ci_95']:.1%}], "
                     f"P(Sharpe <= 0) = {s['prob_sharpe_le_0']:.2f}")
    return lines


def _diagnosis_section(d: dict[str, Any] | None) -> list[str]:
    if not d:
        return []
    a = d["attribution"]
    lines = ["", "## Attribution", "", "| item | value |", "|---|---|"]
    for key, fmt in (("gross_total_return", "{:.2%}"), ("net_total_return", "{:.2%}"), ("cost_drag", "{:.2%}"),
                     ("gross_sharpe", "{:.2f}"), ("beta", "{:.2f}"), ("alpha_annual", "{:.2%}"),
                     ("correlation", "{:.2f}"), ("avg_exposure", "{:.2%}"), ("round_trips_per_year", "{:.1f}"),
                     ("avg_bars_held", "{:.1f}"), ("positive_month_fraction", "{:.0%}"),
                     ("best_month_share_of_log_return", "{:.0%}"), ("top_decile_trips_share_of_pnl", "{:.0%}")):
        if a.get(key) is not None:
            lines.append(f"| {key} | {fmt.format(a[key])} |")
    reg = a["regimes"]
    lines += ["", f"Regimes (ex-post, {reg['lookback_bars']}-bar lookback; description only):", "",
              "| regime | share of bars | strategy log return | asset log return | strategy sharpe |",
              "|---|---|---|---|---|"]
    for name in ("trend_up", "trend_down", "vol_high", "vol_low"):
        r = reg[name]
        lines.append(f"| {name} | {r['fraction']:.0%} | {r['strategy_log_return']:+.3f} | "
                     f"{r['asset_log_return']:+.3f} | {r['sharpe']:.2f} |")
    lines += ["", "## Diagnosis", ""]
    if not d["labels"]:
        lines.append("No failure pattern detected by the rule set.")
    for x in d["labels"]:
        lines.append(f"- **{x['label']}**: {x['evidence']}. {x['suggestion']}")
    return lines


def write_experiment_report(paths: Paths, store: ExperimentStore, exp_id: str) -> Path:
    rec = store.get_experiment(exp_id)
    if rec is None:
        raise KeyError(exp_id)
    out_dir = paths.reports / exp_id
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(paths, rec["dataset_id"])
    lines = [f"# Experiment {exp_id}", ""]
    lines += [f"- Status: **{rec['status']}** ({rec['kind']}, segment `{rec['segment']}`)",
              f"- Strategy: `{rec['strategy_ref']}` parameters `{json.dumps(rec['parameters'])}`",
              f"- Candidate hash: `{rec['candidate_hash']}`; strategy hash: `{rec['strategy_hash']}`",
              f"- Dataset: `{rec['dataset_id']}` (data hash `{rec['data_hash']}`)",
              f"- Split: `{rec['split_id']}`; engine `{rec['engine_version']}` config `{rec['engine_config_id']}`",
              f"- Costs: `{json.dumps(rec['engine_config']['costs'])}`"]
    if rec["window_start"]:
        lines.append(f"- Scored window: {to_iso(rec['window_start'])} -> {to_iso(rec['window_end'])}")
    trials = store.trial_count(split_id=rec["split_id"])
    lines.append(f"- Cumulative research trials on this split (all sessions): {trials}")
    if rec["notes"]:
        lines.append(f"- Note: {rec['notes']}")
    if rec["failure_reason"]:
        lines += ["", "## Failure", "", "```", rec["failure_reason"], "```"]

    if rec["status"] in ("completed", "duplicate") and rec["metrics"]:
        m, b = rec["metrics"], rec["benchmarks"]
        pm = b.get("policy_matched")
        lines += ["", "## Metrics", ""]
        if pm:
            lines += [f"Portfolio policy `{rec['checks'].get('policy_applied', rec.get('policy_id'))}`; "
                      f"policy-matched = same policy and costs on a constant proposal of {pm['proposal']:.2f}.", ""]
        lines += ["| metric | strategy | buy & hold | policy-matched | cash |", "|---|---|---|---|---|"]
        for key in ("total_return", "cagr", "sharpe", "max_drawdown"):
            lines.append(f"| {key} | {_fmt(key, m[key])} | {_fmt(key, b['buy_and_hold'][key])} | "
                         f"{_fmt(key, pm[key]) if pm else ''} | {_fmt(key, b['cash'][key])} |")
        for key in ("ann_volatility", "sortino", "calmar", "n_fills", "n_round_trips", "win_rate", "turnover",
                    "exposure", "total_fees", "total_slippage", "final_equity", "liquidation_equity", "bars"):
            if key in m:
                lines.append(f"| {key} | {_fmt(key, m[key])} | | | |")
        lines += _statistics_section(rec["statistics"]) + _diagnosis_section(rec["diagnosis"])
        lines += ["", "## Checks", "", "```json", json.dumps(rec["checks"], indent=2), "```"]
        equity = pd.read_parquet(Path(rec["result_dir"]) / "equity.parquet")
        _chart(equity, rec["engine_config"]["initial_cash"], out_dir / "equity.png",
               f"{rec['strategy_ref']} {rec['segment']}")
        lines += ["", "![equity](equity.png)"]

    lines += ["", "## Data and assumptions", "",
              f"- Venue `{manifest['venue']}` {manifest['instrument']} {manifest['granularity']}; "
              f"coverage {manifest['coverage'] * 100:.2f}% of expected bars, {manifest['internal_gap_bars']} "
              f"internal missing bar(s) in {len(manifest['gaps'])} gap(s); fill policy `{manifest['fill_policy']}` "
              f"({manifest['filled_rows']} filled).",
              f"- Actual data range: {to_iso(manifest['actual_first_open'])} -> {to_iso(manifest['actual_last_open'])}"
              " (bar opens, UTC).",
              "- Signals use completed bars only and fill at the next bar open with market orders; real fills can "
              "differ (spread, latency, partial fills). Limit-order fills are not modelled.",
              "- Fees/slippage are configuration inputs; unless the cost source says otherwise they are not the "
              "account's verified tier.",
              "- Results are on historical data of this venue only and do not demonstrate capacity or live "
              "profitability."]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def _pricing_line(sess: dict[str, Any]) -> str:
    p = (sess.get("config") or {}).get("pricing")
    u = sess["usage"]
    if not p:
        return "- Model cost: unknown (no pricing configured)"
    cost = f"${u['model_cost_usd']:.4f}" + ("" if u.get("model_cost_known", True) else " (incomplete)")
    return (f"- Model cost: {cost} for {p['model']}; prices per 1M tokens in {p['currency']}: input "
            f"{p['input_per_mtok']}, cache hit {p.get('cache_read_per_mtok')}, output {p['output_per_mtok']}, "
            f"schedule `{p.get('schedule', 'flat')}`; source: {p.get('source') or '-'}")


def write_session_report(paths: Paths, store: ExperimentStore, session_id: str, summary: dict[str, Any]) -> Path:
    sess = store.get_session(session_id)
    out_dir = paths.reports / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    exps = store.list_experiments(limit=10_000, session_id=session_id)
    from quant_research.research.mandate import Mandate

    mandate = Mandate.model_validate(sess["config"]["mandate"])
    lines = [f"# Research session {session_id}", "", f"- Question: {sess['question']}",
             f"- Dataset `{sess['dataset_id']}`, split `{sess['split_id']}`",
             f"- Mandate `{mandate.mandate_id()}`: design `{mandate.evaluation_design.model_dump_json()}`, "
             f"primary metric `{mandate.primary_metric}`, acceptance `{mandate.acceptance.model_dump_json()}`",
             f"- Final state: **{sess['state']}** ({sess['stop_reason']})",
             f"- Budget: `{json.dumps(sess['budget'])}`", f"- Usage: `{json.dumps(sess['usage'])}`",
             _pricing_line(sess),
             f"- Experiments recorded in this session: {len(exps)}; cumulative trials on split: "
             f"{store.trial_count(split_id=sess['split_id'])}", ""]
    best = summary.get("best")
    if best:
        plan_note = ("retrospective: written by the controller after the submission"
                     if best.get("plan_retrospective") else best.get("plan_status"))
        lines += ["## Best accepted candidate", "", f"- `{best['strategy_ref']}` `{json.dumps(best['parameters'])}`",
                  f"- candidate `{best.get('candidate_id')}`, plan `{best.get('plan_id')}` ({plan_note})",
                  f"- validation experiment `{best['validation_experiment']}`, sharpe {best['validation_sharpe']:.3f}",
                  f"- Next step (human): `zqr candidate freeze {best.get('candidate_id')}` then "
                  f"`zqr holdout-test --candidate {best.get('candidate_id')}`; the agent cannot access holdout "
                  "data.", ""]
    else:
        lines += ["## Outcome", "", "No candidate met the acceptance criteria. This is a valid research result.", ""]
    candidates = summary.get("candidates", [])
    lines += ["## Candidates", "",
              "| round | strategy | params | plan | dev sharpe | val sharpe | val DSR | cost drag | labels | verdict | "
              "reasons |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in candidates:
        drag = f"{c['cost_drag']:.1%}" if c.get("cost_drag") is not None else "-"
        plan = "retrospective" if c.get("plan_retrospective") else (c.get("plan_status") or "-")
        lines.append(f"| {c['round']} | `{c['strategy_ref'][:24]}` | `{json.dumps(c['parameters'])}` | {plan} | "
                     f"{_fmt('sharpe', c.get('dev_sharpe'))} | {_fmt('sharpe', c.get('validation_sharpe'))} | "
                     f"{_fmt('dsr', c.get('validation_dsr'))} | {drag} | {', '.join(c.get('labels') or [])} | "
                     f"{c['verdict']} | {'; '.join(c.get('reasons', []))} |")
    counts: dict[str, int] = {}
    for c in candidates:
        for label in c.get("labels") or []:
            counts[label] = counts.get(label, 0) + 1
    if counts:
        lines += ["", "## Failure patterns", ""]
        lines += [f"- `{k}` in {v} candidate(s)" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    statuses: dict[str, int] = {}
    plan_counts: dict[str, int] = {}
    sensitivity = 0
    for e in exps:
        statuses[e["status"]] = statuses.get(e["status"], 0) + 1
        if e["kind"] in ("sensitivity", "walkforward_fold"):
            sensitivity += e["kind"] == "sensitivity"
            continue
        key = e.get("plan_status") or "unplanned"
        plan_counts[key] = plan_counts.get(key, 0) + 1
    plans = store.list_plans(session_id)
    lines += ["", "## Plans", "",
              f"- {sum(not p['retrospective'] for p in plans)} preregistered plan(s), "
              f"{sum(bool(p['retrospective']) for p in plans)} retrospective plan(s) written by the controller for "
              "candidates submitted without one",
              f"- Experiments by plan status: `{json.dumps(plan_counts)}`",
              f"- Sensitivity reruns (recorded, not counted as trials): {sensitivity}"]
    lines += ["", f"Experiment outcomes in this session: `{json.dumps(statuses)}` (blocked attempts are not "
              "counted as trials; duplicates reuse an earlier result and are counted)."]
    for r in summary.get("agent_reports", []):
        lines += ["", f"## Agent notes, round {r['round']}", "", r["report"]]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n")
    return path
