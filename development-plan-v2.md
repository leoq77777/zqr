# Personal Systematic Research Desk — Architecture and Development Plan v2

Date: 2026-10-12. Status: proposed design, not implemented. Supersedes v1 architecture decisions; the original development-plan.md remains the historical API investigation record.

## 1. Objective and scope

Build a reproducible research desk for a personal trader with limited compute. The primary deliverable is a defensible research decision, not a daily trading recommendation. The Agent investigates and implements candidates; deterministic programs calculate results, enforce rules, and eventually run approved strategies.

Initial market: Coinbase BTC-USD and ETH-USD spot, long/cash, hourly and daily completed bars. First implementation uses one asset; the next milestone tests two-asset aggregation. No leverage, shorting, order-book strategies or HFT. These defaults are engineering scope, not a claim of profitable suitability. Account eligibility remains unresolved.

Research methods are inspired by publicly described systematic investing practices at AQR/Man AHL and scientific investment research at Two Sigma. This does not replicate their proprietary strategies or internal systems.

## 2. Material changes from v1

| v1 emphasis | v2 change | Reason |
|---|---|---|
| Generate a strategy and improve its backtest | Hypothesis -> preregistered experiment -> evidence -> decision | Reduce unstructured search and selection bias |
| Strategy directly produces an account target | Strategy proposes bounded exposure; portfolio policy owns sizing and aggregation | Separate alpha from risk and portfolio choices |
| Risk primarily introduced near paper trading | Same versioned portfolio limits in research and paper | Measure the behavior actually intended for deployment |
| Single-strategy headline metrics | Exposure-matched baselines, turnover, cost sensitivity and contribution | Avoid crediting leverage, sizing or one market regime to signal quality |
| Flexible agent choice | Two thin adapters, common tools, benchmark-based selection | Avoid unsupported framework rankings |
| Strategy files plus Git | Minimal immutable candidate registry and explicit promotion records | Freeze the exact artifact evaluated and deployed |
| Add resources when needed | Fixed single-host budgets and measured performance | Make limited compute a real design constraint |

The added responsibilities are modules in one repository, not separate network services or additional Agents.

## 3. Architecture

```text
User: research question + constraints
                 |
       Research Controller / State
                 |
       Agent Adapter (one active researcher)
                 |
       Common Research Tool Gateway
          /                   \
Source snapshots          Experiment request
                               |
                         Experiment gate
                               |
Coinbase -> Raw capture -> Approved dataset view
                               |
                       Isolated strategy worker
                               |
                      Exposure proposal series
                               |
                      Portfolio / risk policy
                               |
                      Target portfolio series
                               |
                      Backtest accounting engine
                               |
                      Evaluation + validation
                               |
                  Experiment store + research report
                               |
                     Human promotion decision
                               |
                      Frozen candidate registry
                               |
               Paper runtime -> reconciliation + monitoring
```

Actual holdout data resides outside researcher mounts. Agent, strategy worker and final evaluator have distinct filesystem views. On one host this can be containers/processes with host-enforced access controls. Filename conventions and prompt instructions are not an access boundary.

## 4. Core responsibilities and contracts

### Research controller and Agent

Controller owns research lifecycle, budget, allowed sources/data, experiment identity and recovery. One researcher uses the portable benchmark/role_prompt.md. Persist plans, edits, failed attempts and decisions outside chat memory. Scheduled research is optional; no requirement to manufacture daily signals.

First adapters: Claude Agent SDK and original Codex via a supported programmatic interface. No source fork initially. Integration capabilities and exact versions must be checked at implementation. Adapter differences are logged; evaluation distinguishes product/model combinations from same-model framework tests.

Tools: read_source, inspect_dataset, submit_experiment, get_experiment, compare_experiments, build_report. Tool arguments use opaque approved data/config IDs; they cannot request arbitrary host paths or change trusted costs. Backtests do not call a model. Strategy experiments run without model credentials or network.

### Data and source catalog

Preserve raw responses, pagination windows and retrieval time. Clean into immutable Parquet snapshots with venue, product, UTC interval, available_at, quality report and content hash. Exclude incomplete bars; handle range boundaries, duplicates and gaps explicitly. Never silently relabel another venue's data as Coinbase.

Timestamp policy includes bar close availability and excludes acting on data unavailable at the decision time. News/filings are later extensions requiring publication and revision timestamps. Historical availability reconstructed from OHLC is an assumption to document, not a recorded live arrival time.

Development, validation and final evaluation use physically restricted derived views. Reusing validation results for selection is allowed only with logged trial accounting; it is not independent evidence. Record holdout consumption; a failed final test does not reset the same holdout to unseen.

### Strategy and portfolio policy

Strategy contract: generate_exposures(history, parameters) -> timestamped exposure proposals in [0,1], computed only from completed known bars. Initial strategies: simple time-series momentum/trend and a separately justified mean-reversion baseline. Templates and frozen Python plugins share this contract.

Portfolio policy converts proposals into weights using lagged volatility estimates, explicit per-asset caps, aggregate gross cap <= 1 and cash remainder. Version every choice, including warmup, missing data behavior, rebalance schedule and optional no-trade bands. A volatility target is not a guarantee of realized risk. Portfolio parameters are research choices and count as trials when tuned; Agent cannot change authorized limits.

Apply the same policy to single-asset research, combined portfolios and paper. Compare the signal with equivalent-sizing baselines to distinguish alpha from exposure changes. Two crypto assets are not assumed independent; report realized co-movement and overlapping strategy exposure.

Aggregate strategy proposals before orders; opposing proposals must not create unnecessary crossing trades. Start with equal fixed strategy allocations. Portfolio optimizer and learned allocations are deferred until evidence justifies their estimation risk.

### Backtest and evaluation

One fixed, auditable cash/position accounting engine. Signals generated at a bar close execute no earlier than the next open with configured execution assumptions. Define fee debit, slippage, rounding, insufficient cash and final marking before implementation. Test repeated rebalancing and fee-adjusted sizing with hand-calculated cases.

Bar-open fills are research approximations. Sensitivity tests must include worse execution prices/delay assumptions where applicable; historical bars alone cannot establish live fills. Baseline is taker-style market rebalancing; do not assume maker fees for unmodeled limit fills.

Reports include net returns, drawdown, exposure, volatility, turnover, trade count, fee/slippage drag, period results, parameter sensitivity and benchmark comparison. Initial benchmarks: cash (zero interest explicitly assumed), buy-and-hold, and policy-matched passive exposure. Benchmark construction is frozen with the experiment, including allocations and rebalance costs.

For combined candidates report standalone and incremental portfolio behavior, overlapping exposures and concentration of profits. Cost cases are fixed before comparison; actual fees remain account dependent. Statistical uncertainty and trial count accompany point estimates; advanced selection-adjusted tests can be added once sufficiently complete trial records exist.

### Candidate promotion and paper

Registry is SQLite plus immutable artifact directories, not a new service. Candidate identity binds code/spec, dependencies, data, split, engine, sizing and cost configuration. Lifecycle:

draft -> development -> validated -> frozen -> final_evaluated -> paper_approved -> paper_active -> retired

Rejected/failed states and evidence are preserved. Eligibility for promotion requires correctness, reproducibility, adequate evidence and human approval. Numerical thresholds, observation duration and necessary samples are predeclared for each research mandate; v2 does not invent a universal Sharpe cutoff or promise profit.

Paper uses real-time public Coinbase data with a local simulator. Reuse signal/policy semantics, but use an explicit event-driven runtime for late/missing data, restart and clock behavior. Backtest/live parity means reproducible decisions on the same inputs, not identical fills.

Paper controls include staleness checks, exposure limits, request identity, unknown-order state, reconciliation and restart recovery. Failure stops new orders; do not automatically liquidate during ambiguous state. Actual live execution and authenticated-account acceptance are separate future work.

## 5. Technology and deployment

Python, NumPy/pandas, PyArrow/Parquet, Pydantic, SQLite and Git; Markdown and Matplotlib reports. One repository, one host, one active researcher, initially one backtest worker. Measure before increasing concurrency. Container or equivalent isolation is mandatory before Agent-authored Python execution.

No mandatory C++, Spark, Kubernetes, distributed scheduler, vector database or notebook server. No GPU required for remote model inference; local ML experiments may change resource needs. C++/Numba only after hotspot profiling and a quantified performance target, retaining independent correctness cases.

Optional evaluated tools: PyBloqs for rendering, D-Tale for human inspection, Perspective for a later dashboard. None becomes a benchmark prerequisite. ArcticDB, GS Quant and institution-specific concurrency tools remain deferred.

Always-on paper runtime should have supervision, time synchronization, structured logs, disk limits and tested backups. Research may run only on demand. Model credentials belong only to the Agent execution path; trading credentials are not installed for the initial research/paper phases.

## 6. Development sequence and gates

| Milestone | Build | Acceptance gate |
|---|---|---|
| M0 Contracts | Scope, account assumptions, timestamp/fill/cost/policy rules, frozen interfaces | Written hand-worked examples and protected-data layout approved for implementation |
| M1 Data | Coinbase import, cache, resume, quality and restricted snapshots | Requested historical coverage actually measured; gaps disclosed; repeat import stable; old snapshots unchanged |
| M2 Deterministic core | One-asset strategy, portfolio policy, accounting, metrics, baselines | Golden accounting, prefix causality, costs, fee-adjusted weights, replay and future-data isolation pass |
| M3 Research workbench | Experiment gate/store, plans, trial log, validation and report | Manual research end-to-end; rejected/failed trials retained; final holdout controlled |
| M4 Agent evaluation | Claude/Codex adapters, common tools, benchmark grader and fault injection | B02/B03/B05/B07 initial screen; 8-case repetitions for selection; no unresolved serious violations |
| M5 Portfolio research | BTC/ETH multi-asset targets, aggregation, risk caps, contribution | Combined ledger correct; no allocation lookahead; exposure/cost comparisons and concentration report |
| M6 Paper | Final evaluation, explicit approval, streaming, simulator, monitoring, reconciliation | Decision replay parity; stale-data/duplicate-event/restart/unknown-state drills pass; mandate-specific forward review |

Agent integration follows a trustworthy manual workbench. Multi-asset complexity follows a correct single-asset engine. No paid model benchmark or real order is executed by drafting this plan.

Milestones are evidence-based, not time estimates. Before starting implementation set a total development scope and a model spending ceiling. Framework comparisons include failed-run costs, unavailable billing fields and human interventions.

## 7. Benchmark updates

Existing benchmark is a prepared public development suite, not a functioning model comparison. Keep B01-B08. Add after the portfolio milestone:

- B09 sizing attribution: determine whether an apparent improvement is explained by changed exposure; use matched policy baselines.
- B10 portfolio overlap: combine correlated signals/assets under aggregate caps and distinguish diversification from duplicated risk.
- B11 cost-aware rebalance: identify overtrading introduced by sizing; compare predeclared rebalance/no-trade policies and count these as trials.

Do not quietly replace v0.1's target-weight interface: initial adapter benchmarking can retain v0.1, while v2 uses a versioned exposure contract. Before M4 uses the new engine, publish a v0.2 benchmark contract and translate B02 expected exposures versus final targets explicitly. The current fixtures/graders cannot be claimed compatible without that work.

Formal ranking uses unpublished tasks/variants, frozen environment and common tools. A profitable backtest is not the harness selection criterion. First prioritize serious-error rate, reproducibility and completion; then cost per qualified delivery and maintenance burden.

## 8. Single-host resource policy and expansion

Download once, cache immutable views, batch deterministic parameter evaluations, and have one database writer. Research budgets cover model cost, candidate count, wall time, CPU, memory and disk. Tool submissions carry idempotency identity. The controller cannot enforce arbitrary shell calls merely through prompts; worker resource limits must be OS enforced.

First performance workload: available five-year hourly history for two products and 100 deterministic candidates, with actual coverage disclosed. Record CPU, peak memory, total time and correctness; set an acceptable target after observing the host. No unmeasured throughput promise.

Only migrate to PostgreSQL/object storage/remote workers if concurrency, queue or data limits justify it. Keep experiment and candidate contracts stable. Low latency/HFT is a separate market-data, simulation and execution project, not a routine upgrade of this bar-based system.

## 9. Current evidence and unresolved decisions

Already available: v1 design/API investigation; successful unauthenticated BTC/ETH historical-hour samples; benchmark prompts, task definitions, synthetic fixtures and schemas. Not available: full historical coverage validation, operational research engine, sandbox worker, adapters, auto grader, framework ranking or paper runtime.

Resolve before implementation: operating host/OS, account jurisdiction, capital/risk mandate, requested history, compute and model budget, and paper observation requirements. These do not block preparing research architecture, but actual risk limits and live eligibility cannot be inferred.

## 10. Public references

- [AQR strategies](https://funds.aqr.com/Strategy), [Man AHL](https://www.man.com/ahl), [Two Sigma investment management](https://www.twosigma.com/businesses/investment-management/): methodological inspiration, not internal architecture evidence.
- [Coinbase candles](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/get-public-product-candles), [sandbox](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox): existing integration choice; exact installed/API behavior checked during M1/M6.
- [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview), [Codex app-server](https://learn.chatgpt.com/docs/app-server): first integration candidates, no proven winner.

The complete benchmark preparation protocol remains in benchmark/README.md. v2 is an implementation proposal; no compatibility or profitability claim follows from this document.
