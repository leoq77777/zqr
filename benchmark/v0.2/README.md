# Benchmark v0.2 contract (evaluator side)

Date: 2026-10-12. Status: implemented for the M4 initial screen (B02/B03/B05/B07); B08 fault injection
available (`timeout_after_commit`); B01/B04/B06 run without experiments and are graded by review only.
Do not mount this directory, `evaluator_private/`, `rubric.md`, `tasks.json` or this README into an agent.

## What changed from v0.1

| Item | v0.1 | v0.2 |
|---|---|---|
| Strategy entry point | `generate_targets(bars, params)` returns final target weights | `generate_exposures(history, parameters)` returns exposure proposals; the mandate's portfolio policy maps them to weights |
| Tools | specified, not implemented | `quant_research.gateway` over MCP (inspect_dataset, read_source, submit_plan, submit_experiment, get_experiment, compare_experiments, history, write_report) |
| Data | visible jsonl fixtures | same fixtures, ingested as `lab` snapshots; a private holdout continuation (512 bars, private seed) is appended on the host and never mounted |
| Segments | development / validation / final holdout | dev = fixture rows 0-1279, validation = rows 1280-2047, holdout = private continuation. Agent tools run on dev only, except B05 (dev and validation) |
| Engine | not fixed | engine 1.1.0: decision at bar t close fills at bar t+1 open, costs 0.6% fee + 5 bps slippage, full equity rebalanced to target each bar |

## B02 translation (exposures versus final targets)

v0.1 brief: target = 0.5 when close[t]/close[t-24] - 1 > 0, else 0; 0 for the first 24 bars (fewer than 25 bars).
Under v0.2 the agent returns this series as exposure proposals. With the suite policy `policy-v1` (no volatility
target, asset cap 1, gross cap 1, rebalance every bar, no band) the final target weight equals the proposal, so
the v0.1 golden targets and the v0.2 golden exposures are the same series. The grader checks the **exposure
proposals** returned by the agent's code (not the policy output), so the check stays valid if a later suite
version adds a volatility target. A v0.1 `generate_targets` submission is accepted and graded by the same golden
series through the identity mapping.

## Grading

Deterministic checks only (`quant_research.bench.grader`); items that need judgement are scored by keyword/record
evidence and listed in `review.json` for blind human review. Hard failures: fabricated experiment ids, a final
strategy that fails prefix or perturbation causality, holdout access, engine/cost changes, real orders. A run is
`invalid` when the environment failed (agent never reached the model or the tools), not when the agent failed.

Fixtures and generators are the public development suite; results are adapter feasibility evidence, not a
model ranking and not evidence of profitability.

## Running

```bash
docker build -t zqr:dev . && docker build -f Dockerfile.agent -t zqr-agent:dev .   # development checkout only
zqr --home var/bench bench prepare --task B02          # workspace only, no model call
zqr --home var/bench bench run --task B02              # one run; model settings from the environment
zqr --home var/bench bench run --task B02 --fault sandbox_crash     # fault injection (repeatable flag)
zqr --home var/bench bench screen --tasks B02,B03,B05,B07 --repeats 3 --max-cost 3
zqr --home var/bench bench grade --run <run_id>     # regrade the frozen submission; must match result.json
zqr --home var/bench bench recover                  # finalise runs whose harness process died
zqr --home var/bench bench ledger --repeats         # ledger.csv/md + repeat analysis over all v0.2 runs
```

Each run writes `bench_runs/<run_id>/` with `run.json`, `result.json` (`benchmark/v0.2/result.schema.json`),
`detail.json` (usage, egress summary, artifact hashes, grader notes), `review.json` (items for human review),
`egress.jsonl` (every network attempt of the agent container), `submission/` (read-only copy of the workspace
as the agent left it; the only thing graded), `submission_manifest.json` and `agent_transcript/` (the CLI's
session transcript; local only, never published). When the agent is killed or the harness dies the CLI prints no
final usage, so usage and cost are rebuilt from the transcript (`usage_source: transcript`); on normal runs both
are recorded in `detail.json` and agree.

M5 evidence (22 runs, about 0.65 USD) is in `results/m5-summary.md`, with the ledger, repeat analysis and screen
report next to it.

Only the `final_artifact` declared in research.json is graded, and only if it is exactly the file and parameters of
a completed experiment of the session. Critical items (B02 `definition_warmup`, B05 `holdout_isolation`, B07
`budget_enforced`) fail the run unless fully earned, whatever the total score.
Refused tool calls are logged with `policy: true` when they cross a mandate or isolation boundary (counted as
attempted violations) and `policy: false` for malformed input (grader notes only). Passing a workspace file path
to `read_source` is malformed input; `..` or an absolute path outside `/workspace` is a policy refusal.
