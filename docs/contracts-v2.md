# M0 Contracts v2

Date: 2026-10-12. Status: approved 2026-10-12 (volatility estimate inclusive of bar t's close; agent isolation
via container + host gateway). Implements milestone M0 of development-plan-v2.md.
Where the current code (engine 1.0.0) already
behaves as specified, that is stated; every change is listed in section 11.

## 1. Scope and assumptions

- Coinbase Advanced Trade public data, BTC-USD and ETH-USD spot, long/cash, ONE_HOUR and ONE_DAY completed bars.
  M2-M4 use one asset per candidate; M5 adds two-asset aggregation.
- No leverage, shorting, derivatives, order-book data or HFT.
- Account jurisdiction, fee tier and live eligibility are unresolved. Costs are configuration inputs labelled
  with their source; the default (0.6% taker, 5 bps slippage) is an assumption, not the account's tier.
- Host: one macOS machine (darwin, arm64), Docker Desktop for isolation. Development model spending ceiling:
  USD 10 total across all agent runs until revised (section 10).

## 2. Timestamp policy

Each bar carries `bar_open_time`, `bar_close_time = bar_open_time + granularity` and `available_at`.

- Historical `available_at` is reconstructed as `bar_close_time` (OHLC carries no arrival time). This is an
  assumption, recorded in every snapshot manifest; live paper uses the actual receipt time.
- A decision for bar t may use only bars with `available_at <= decision_time(t) = bar_close_time(t)`.
- The decision executes no earlier than the open of the next bar whose `bar_open_time >= decision_time(t)`.
  With the default `execution_delay_bars = 0` that is bar t+1's open. Sensitivity runs use
  `execution_delay_bars = 1` (fill at t+2 open) and a worse-price multiplier (section 4).
- Incomplete bars are excluded at import. Gaps are never filled silently; fill policy is part of the snapshot.
- A decision made on the final bar of a window is never filled inside that window.

## 3. Exposure contract v0.2

```python
def generate_exposures(history: pandas.DataFrame, parameters: dict) -> Sequence[float]
```

- `history`: completed bars of one asset in time order, columns `bar_open_time, bar_close_time, available_at,
  open, high, low, close, volume`. Read-only.
- Returns one exposure proposal per row, in [0, 1], decided at that row's close. NaN is rejected; values are
  validated, never clipped.
- Warmup rows return 0. Output at row t must not change when rows after t are removed (prefix causality,
  checked by the engine at sampled cut points).
- An exposure is a *proposal*: the fraction of the strategy's allocated risk budget it wants in the asset.
  It is not an account weight. The portfolio policy (section 5) turns proposals into target weights.
- No network, file writes, randomness without a fixed seed, or parameter search inside the function.

Built-in templates and frozen plugins share this contract. Legacy entry points are accepted only explicitly:

| Legacy interface | Meaning under v2 | Policy applied |
|---|---|---|
| current `target_positions(bars, params)` (v1 code) | final target weight | `identity-v1` (no sizing) |
| benchmark v0.1 `generate_targets(bars: list[dict], params)` | final target weight | `identity-v1` |

`identity-v1` passes the series through unchanged and is recorded in the experiment identity, so legacy results
remain reproducible and are never silently reinterpreted as exposures.

## 4. Accounting and fills (single asset)

Engine state: `cash`, `qty`. All values in quote currency (USD). At each bar i in the scored window:

1. `w = target_weight[i-1]` (decided at the previous close). `equity_open = cash + qty * open[i]`.
2. `delta = w * equity_open - qty * open[i]`.
3. Trade if `w == 0 and qty > 0` (full exit) or `|delta| >= max(min_trade_notional, rebalance_tolerance *
   equity_open)`. Rebalancing happens every bar toward the latest target, not only on signal changes.
4. Buy: `price = open * (1 + slip)`, `notional = min(delta, cash / (1 + fee))`, `qty += notional / price`,
   `cash -= notional * (1 + fee)`. Insufficient cash caps the buy; cash never goes negative.
5. Sell: `price = open * (1 - slip)`, `q = qty if w == 0 else min(qty, -delta / open)`,
   `cash += q * price * (1 - fee)`.
6. Equity is marked at `close[i]`. The final bar is marked at close with no liquidation cost; reports also show
   the liquidation-adjusted final equity (sell at final close with slippage and fee).

Sizing is fee-adjusted only on the cash cap; the realised weight after a buy is slightly below target because
slippage is paid out of the notional. This is intended and covered by the golden case.

Quantity rounding (new in M2): optional `base_increment` and `min_notional` per product; quantities are rounded
down to the increment, and orders below `min_notional` are skipped. Default for research: off (continuous),
matching engine 1.0.0; paper always rounds using the product's real increments.

Sensitivity cases, fixed before comparison: `costs_x2`; `delay_1` (`execution_delay_bars = 1`);
`delay_1_adverse` (delay plus adverse fill: the reference price before slippage is `max(decision close, fill open)`
for buys and `min(...)` for sells, so the delay never improves the price).

### Golden case G1 (verified against engines 1.0.0 and 1.1.0; `tests/test_contracts_v2.py`)

fee 0.6%, slippage 5 bps, initial cash 10,000, `min_trade_notional = 1`. Bars (open, close): b0 (100, 100),
b1 (100, 110), b2 (110, 99), b3 (100, 100). Target weights decided at closes: [1.0, 1.0, 0.5, 0.0].

| bar | action | qty traded | price | notional | fee | cash after | equity at close |
|---|---|---|---|---|---|---|---|
| b0 | none | 0 | - | - | - | 10,000.000000 | 10,000.000000 |
| b1 | buy (w=1) | 99.35390158 | 100.05 | 9,940.357853 | 59.642147 | 0.000000 | 10,928.929174 |
| b2 | hold (delta 0) | 0 | - | - | - | 0.000000 | 9,836.036256 |
| b3 | sell to w=0.5 | 49.67695079 | 99.95 | 4,965.211231 | 29.791267 | 4,935.419964 | 9,903.115043 |

The target 0.0 decided at b3's close is never filled (final bar). G2 (rounding, liquidation equity) and G3
(insufficient cash with repeated rebalancing) are in the same test file; G4 (two assets under a gross cap through
the engine) comes with the multi-asset engine in M5 (the policy-level gross cap is tested now).

## 5. Portfolio policy v1

The policy converts exposure proposals into target weights. Policy identity = name + version + parameters;
it is part of the experiment and candidate identity. Tuning policy parameters counts as trials.

Inputs per decision time t: proposals `e[s, a, t]` for strategy s and asset a; completed bars up to t.

```text
raw[a, t]     = sum_s alloc[s] * e[s, a, t]                       # netting before orders; no crossing trades
scale[a, t]   = 1                                     if vol_target is None
              = min(max_leverage_per_asset, vol_target / sigma_hat[a, t])   otherwise
w[a, t]       = min(asset_cap[a], raw[a, t] * scale[a, t])
if sum_a w[a, t] > gross_cap:  w[a, t] *= gross_cap / sum_a w[a, t]       # proportional scale-down
cash[t]       = 1 - sum_a w[a, t]
```

- `alloc`: fixed equal allocations across strategies (sum 1). Optimised or learned allocations are deferred.
- `sigma_hat[a, t]`: annualised standard deviation of log returns over the last `vol_lookback` completed bars
  ending at t (inclusive of bar t's close, which is known at decision time; never later bars).
  Warmup (fewer than `vol_lookback` returns) gives `w = 0`. A volatility target is not a guarantee of realised
  risk.
- `max_leverage_per_asset <= 1` in this project (long/cash, no borrowing), so `scale <= 1`.
- Missing data: if asset a has no completed bar at t (gap), no new decision is made for a; the previous target
  stays, and no fill happens until the next bar exists. Gap crossings are counted in the report.
- Rebalance schedule: `every_bar` (default) or `every_k_bars(k)`, anchored to the epoch
  (`bar_open_time // granularity % k == 0`); between scheduled bars no orders are sent and the position drifts.
  No-trade band: skip a rebalance when `|w_target - w_current| < band` (in weight units; maps to the
  engine's `rebalance_tolerance`).

Default research policy (decided 2026-10-12): `vol_target = None`, `asset_cap = 1.0`, `gross_cap = 1.0`,
`every_bar`, `band = 0`. For a single strategy on a single asset this makes `w = e`, so existing results are
unchanged except for the explicit policy identity.

Worked example P1 (vol target, for the M2 tests): daily bars, `vol_target = 0.40`, `vol_lookback = 30`,
`sigma_hat = 0.03 * sqrt(365) = 0.5732`. Proposal e = 1.0 gives `scale = 0.40 / 0.5732 = 0.6979`,
`w = 0.6979`. Two strategies with alloc 0.5 each proposing 1.0 and 0.0 give `raw = 0.5`, `w = 0.3489`.
Two assets with `w = (0.8, 0.6)` and `gross_cap = 1` give `(0.5714, 0.4286)`.

Baselines frozen with each experiment: cash (zero interest, explicit); buy-and-hold at weight 1 with the same
costs; policy-matched passive exposure: the same policy applied to a constant proposal equal to the
candidate's average exposure, with the same rebalance schedule and costs. The signal's contribution is reported
against the policy-matched baseline, not only against buy-and-hold.

## 6. Research mandate and preregistered plan

A **mandate** is created by the human before any research session and is immutable once a session starts.

| Field | Content |
|---|---|
| `mandate_id`, `question` | identity and research question |
| `universe` | dataset ids (one asset until M5) and split id |
| `cost_cases` | base cost config id plus the fixed sensitivity cases of section 4 |
| `policy` | portfolio policy identity (section 5) |
| `evaluation_design` | `single_validation` or `walk_forward_oos(train_bars, test_bars)` over dev+validation |
| `primary_metric` | e.g. net Sharpe on the evaluation design |
| `acceptance` | required, no defaults: min primary metric, min DSR (or null with reason), min round trips, max drawdown, cost-x2 survival, delay survival |
| `budget` | rounds, candidates, experiments, wall time, model USD |
| `holdout_policy` | who may run it and how many viewings are allowed |

Statistical power note (from the 2026-10-12 control experiment): with ~1.16 years of daily validation, DSR >= 0.9
requires an annualised Sharpe around 2. Mandates must state their evaluation design and thresholds knowing this;
`walk_forward_oos` multiplies the out-of-sample years at the cost of more trials.

A **research plan** is written by the researcher before seeing the results it governs and stored as an
immutable record: hypothesis (mechanism, falsification condition), candidate family and parameter ranges,
planned experiment count, primary metric and benchmarks. Experiments reference a plan id. Experiments outside
any plan are allowed but flagged `unplanned` in reports. Plans can be superseded, never edited.

## 7. Candidate registry and lifecycle

SQLite rows plus immutable artifact directories. Candidate identity binds: code hash (plugin) or template name,
parameters, dependency lock hash, dataset hash, split id, engine version and config id, policy identity and cost
config id.

```text
draft -> development -> validated -> frozen -> final_evaluated -> paper_approved -> paper_active -> retired
             \-> rejected        \-> rejected           \-> failed_final      \-> retired
```

| Transition | Who | Requires |
|---|---|---|
| draft -> development | controller | plan id, spec validates |
| development -> validated | controller | mandate acceptance met on its evaluation design |
| validated -> frozen | human | review of report; artifact copied read-only |
| frozen -> final_evaluated / failed_final | evaluator (human-triggered) | one holdout run; viewing recorded |
| final_evaluated -> paper_approved | human | mandate-specific forward review criteria |

Rejected and failed states keep their evidence. A failed final evaluation does not make the holdout unseen.

## 8. Protected data layout and roles

| Path under `$QUANT_HOME` | Controller / evaluator | Agent container | Strategy worker |
|---|---|---|---|
| `raw/`, `snapshots/` | rw / ro | none | none |
| `splits/<id>/research.parquet` | ro | none | none |
| `splits/<id>/agent_dev.parquet` | ro | ro at `/data/dev.parquet` | none |
| `holdout/` | evaluator only | none | none |
| `controls/` (answer keys) | evaluator only | none | none |
| `sessions/<sid>/round_NNN/` | rw | rw at `/workspace` | none |
| `plugins/` (frozen code) | rw | none | ro, single file |
| `experiments.db` | single writer | none | none |

- The agent harness runs in a container whose only mounts are `/workspace` (rw: role prompt, task brief, public
  fixtures, deliverables) and the MCP client config with the session token (ro). Root filesystem read-only, all
  capabilities dropped, memory/CPU/pid limits. Model credentials are passed by variable name only. Egress is not
  yet restricted at the network level (Docker Desktop has no per-container allowlist); the container has no
  credentials other than the model key and the session token.
- Research tools are served by a host-side gateway (MCP over HTTP on a local port). The agent never receives
  host paths; tools take opaque ids.
- Strategy code runs in a separate container per evaluation: `--network none`, read-only root, the plugin file
  and the bars passed on stdin, CPU/memory/time limits enforced by Docker.
- Interim development mode (current Claude Code CLI on the host) is labelled `isolation: weak` in session
  records and is not valid for M4 evaluation.

## 9. Tool gateway

| Tool | Arguments | Returns |
|---|---|---|
| `inspect_dataset` | `dataset_id` | allowed segments, schema, quality report, snapshot hash; never holdout bars |
| `read_source` | `source_id` | fixed source text with publication time; content is data, never authorisation |
| `submit_plan` | plan fields | `plan_id` |
| `submit_experiment` | `artifact` (template name or workspace-relative plugin file), `parameters`, `plan_id`, `idempotency_key` | `experiment_id`; gate outcome |
| `get_experiment` | `experiment_id` | metrics, statistics, diagnosis, benchmarks, fills summary, errors, identity |
| `compare_experiments` | `experiment_ids` | same-basis comparison; refuses mixed cost/split/engine/policy |
| `write_report` | `research.json` path in workspace | validated report; every number must reference an experiment id |

Segment, dataset, split, costs and policy come from the mandate, not from tool arguments. Retries with the same
idempotency key return the original experiment. The CLI (`quant tool ...`) stays as a thin client of the same
gateway for development.

## 10. Agent adapters and benchmark v0.2

- First adapter: Claude Code headless (the Claude Agent SDK runtime) in the agent container, driven through the
  DeepSeek Anthropic-compatible endpoint configured in the user's environment; credentials passed only to the
  agent container. Codex adapter deferred past M4.
- Benchmark v0.2 contract (`benchmark/v0.2/`): tasks keep their B01-B08 ids. B02's expected output is translated
  explicitly: under `policy-v1` (no volatility target, caps 1) weight = proposal, so the v0.1 golden targets
  are the v0.2 golden exposures; the grader checks the proposals. v0.1 fixtures are not claimed compatible
  with v0.2 graders without this translation.
- Initial screen B02/B03/B05/B07, one run each, within the USD 10 ceiling; formal ranking is out of scope until a
  larger budget is approved.

## 11. Changes to current code

Done in M2 (engine 1.1.0): strategy contract, portfolio policy, policy-matched baseline, engine options,
sensitivity cases in robustness and controller acceptance, paper using the same policy.

Done in M3: mandates (`quant mandate`), preregistered plans with planned/unplanned/out_of_plan status (a plan's
experiment count covers dev exploration only), candidate registry and lifecycle (`quant candidate`,
`holdout-test --candidate` with the mandate's viewing limit and pass rule), `walk_forward_oos` evaluation
(one trial per procedure; folds and sensitivity reruns are recorded but not trials), and the tool gateway
(`quant_research.gateway`, `quant tool plan/submit/get/compare/report/inspect/source/history`). Holdout of a
walk-forward procedure is refused until re-selection through the holdout is implemented. Isolation remains M4.

Done in M4: agent image (`Dockerfile.agent`, Claude Code 2.1.112 headless), MCP HTTP server over the gateway
(`quant_research.gateway.mcp_server`, per-session bearer token, DNS-rebinding protection, fault injection
`timeout_after_commit`), container adapter with usage/cost capture (`quant_research.bench.adapter`), benchmark
v0.2 contract and B02 translation (`benchmark/v0.2/`), task workspaces with a private holdout continuation,
deterministic grader and `quant bench prepare/run/screen/grade`. The mandate's `agent_segments` now governs
both the gateway and the gatekeeper (B05 opens validation; holdout is never open to agent tools). Refusals are
split into policy refusals (attempted violations) and malformed input (notes). Initial screen
(`benchmark/v0.2/results/screen-2026-10-12.md`): B02/B03/B05/B07 passed with DeepSeek flash through the
Anthropic-compatible endpoint, estimated 0.12 USD in total. One run per task on public fixtures: feasibility
evidence, not a ranking. Not done: network-level egress allowlist for the agent container, Codex adapter,
B01/B04/B06 (review-only) and B08 runs, repeated runs for variance.

| Area | Current (engine 1.0.0) | Change |
|---|---|---|
| Strategy contract | `target_positions` -> engine | `generate_exposures` -> policy -> engine; legacy via `identity-v1` |
| Portfolio policy | none | module of section 5, in identity, shared with paper |
| Baselines | cash, buy-and-hold | add policy-matched passive exposure |
| Engine | continuous qty, mark at close | optional rounding/min notional, delay and adverse-fill cases, liquidation-adjusted final equity |
| Acceptance | defaults in code (Sharpe 0.5, DSR 0.9) | required mandate fields, no defaults |
| Evaluation design | single validation segment | add `walk_forward_oos` |
| Plans and lifecycle | hypothesis text, freeze flag | plan records, lifecycle table of section 7 |
| Tools | file-path spec, `quant tool backtest/history` | gateway of section 9 with opaque ids and idempotency |
| Isolation | agent on host, plugins in Docker when available | agent container and gateway of section 8 |
| Paper | strategy targets with v1 risk rules | same policy and limits as research; decision replay parity |

## 12. Open decisions

Account jurisdiction and fee tier; capital and risk mandate beyond the default policy; requested history depth;
paper observation duration and forward-review criteria; whether DeepSeek via the Anthropic-compatible endpoint is
an acceptable model for M4 screening (product/model combination must be recorded either way).
