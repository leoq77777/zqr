# Development Guide — Research Desk (as built)

Status as of 2026-10-13 (ZQR layout, image-based deployment). This document describes what is implemented and
running. The repository root is `/Users/leo/code/Quant/ZQR` on the development Mac. Design
rationale lives in `development-plan-v2.md`; frozen interfaces (timestamps, exposure, accounting, policy,
mandates, registry, protected data) live in `docs/contracts-v2.md`. Where they disagree with the code, the code and
this guide describe the current behaviour.

## 1. What the system is

A personal systematic research desk for Coinbase BTC-USD and ETH-USD spot, long/cash only, hourly bars.

- An AI agent researches strategies around the clock inside an isolated container. It only sees the `dev`
  segment and can only act through a tool gateway.
- Deterministic code decides everything that matters: validation, deflated Sharpe, holdout, promotion,
  paper-trading risk. A model's opinion never changes a candidate's state.
- Under a standing human authorization (`auto_paper`), a candidate that passes validation is frozen,
  holdout-tested and put on paper automatically. **Live trading is never automated** and no trading credentials
  exist anywhere in the system.
- The software ships as versioned Docker images. `zqr release` on the Mac builds them and starts them on a
  Windows PC (WSL2 + Docker Desktop); no source code lives on the PC. A daily report summarises paper results
  and research activity.

## 2. Milestone status

| Milestone | Status | Notes |
|---|---|---|
| M0 Contracts | done | `docs/contracts-v2.md`, golden accounting case in `tests/test_contracts_v2.py` |
| M1 Data | done | Coinbase hourly import, resume, validation, immutable snapshots |
| M2 Deterministic core | done | Strategy templates/plugins, portfolio policy v1, accounting engine, metrics, baselines |
| M3 Research workbench | done | Mandates, preregistered plans, experiment gate/store, candidate lifecycle, holdout control |
| M4 Agent evaluation | done | Agent container, MCP gateway, benchmark v0.2 harness; tasks saturate (see `benchmark/v0.2/results/`) |
| M5 Reliability and security | done | Network-enforced isolation, strict final-artifact grading, fault matrix, audit, cost ledger |
| Desk (closed loop) | done, deployed | Paper fleet, research loop, auto_paper authorization, supervisor, daily report |
| Release (images) | done | Runtime and agent images, `zqr release`, host wrapper `zqr`, desk as a container |
| M6 Portfolio research | phase 3a released | Portfolio versions per product: blend of library members, deadband, drawdown ladder, dev/validation runs, holdout gate, paper portfolio trader, parity, reports, soft stop (section 5.9). Seed portfolios failed (BTC holdout, ETH validation; `docs/reviews/2026-10-15-seed-portfolio-review.md`). Phase 4 (`docs/superpowers/plans/2026-10-15-phase4-campaigns.md`): holdout closure and shadow portfolios released (4a); longer dev (E1: extended datasets, split lineage, inherited K, A13.4) built; registration, gates 1-2 and campaigns not started |
| M7 Paper (full) | partial | Parity on targets, orders and fills, stale-data halt, restart recovery from the bar store are in; reconciliation and drills beyond tests are not |

## 3. Architecture

```text
Mac (development checkout)  --zqr release-->  docker save | ssh quant-wsl docker load
Windows PC: WSL2 (Ubuntu-24.04) + Docker Desktop (starts at sign-in, restarts zqr-desk)
└─ container zqr-desk  (image zqr:<version>, restart unless-stopped, Docker socket, volumes zqr-data + zqr-secrets)
   └─ DeskSupervisor  (desk/supervisor.py; restarts children with backoff; daily report 00:05 UTC)
      ├─ research child: zqr desk research-loop            (only process with model credentials)
      │    per session (sibling containers started through the Docker socket):
      │    ├─ agent container  (zqr-agent:<hash>, Claude Code CLI, read-only rootfs, cap-drop ALL, internal network)
      │    │     └─ egress proxy (zqr:<version>): allows the model host only; forwards egress:8080 -> gateway
      │    ├─ GatewayHTTPServer in zqr-desk (MCP tools, bearer token) -> ToolGateway -> experiment runner (dev only)
      │    ├─ strategy sandbox (zqr:<version>, no network) for agent-authored plugin code
      │    ├─ freeze workspace -> audit -> evaluate declared final_artifact on validation (DSR over all trials)
      │    └─ promote (standing authorization): freeze -> holdout -> paper_approved -> roster entry
      └─ fleet child: zqr paper fleet                        (no model credentials)
           ├─ roster.json hot reload (presets + promoted candidates)
           ├─ one bar store + Coinbase candle poller per product/granularity, one PaperTrader per strategy
           └─ simulated fills (mid + slippage + fees), risk limits, per-strategy ledger and marks, health file
```

Paper history is a bar store per product and granularity (`paper/bars/<product>-<granularity>.parquet`),
anchored at the first bar of the frozen Coinbase dataset reaching furthest back (the latest 17,520 venue bars
when no dataset exists) and only appended. Strategies compute from the whole store, so a restart reproduces
stateful positions. The poller backfills from the last stored bar after an outage; catch-up bars rebuild state and
only the latest bar can trade. Staleness is the age of the latest bar close. Risk limits edited in the roster apply
to running traders; edits to strategy, parameters, policy, capital or costs are refused (add the changed strategy
under a new name). Failed attaches retry with backoff. Every trader marks equity at each bar close; the daily
report computes returns from the marks and checks parity on targets, orders and fills. Paper buys are sized like
the backtest (the target delta at the slipped fill price), capped by cash after fees and the cash buffer and by
`max_order_notional`.

Promotion copies the validated capital and costs into the roster entry, refuses engine settings paper cannot
reproduce (`execution_delay_bars`, `adverse_fill`, `rebalance_tolerance`), does not freeze a candidate on a split
whose holdout viewings are used up, and resumes from any intermediate state (`zqr desk promote <candidate_id>`).
Every holdout path goes through `run_holdout` (`validation/holdout.py`), which enforces the tighter of the
caller's limit (the mandate's for a candidate) and the tightest limit of the split's mandates (default 10), so a
newer mandate cannot reopen a split. A repeat of an already viewed candidate is not a new viewing; over the
limit only an exact repeat runs without an override. `zqr holdout-test --override-reason` (dataset/split path
only) exceeds the limit; the override is recorded in `holdout_overrides` and marks the split's holdout as
contaminated. `zqr close-holdout --split --note` (human, appendix A13.3) closes a split's holdout permanently: every
new viewing is refused (promotion stops before freezing), repeats still answer, and an override lifts the closure
as a recorded contamination.

All state lives under `ZQR_HOME`: `/zqr` (the `zqr-data` volume) in the release container, `./var` in a
development checkout. There is one SQLite database writer per process (WAL, busy timeout).

### 3.1 Two layouts (`runtime.py`)

The same code runs in a development checkout and in the release image; `runtime.py` hides the difference.

| | Development checkout | Release container |
|---|---|---|
| Resources (`benchmark/`, `uv.lock`) | the repository (`ZQR_ROOT` unset) | `/opt/zqr` (`ZQR_ROOT`) |
| Data (`ZQR_HOME`) | `./var` | `/zqr` = volume `zqr-data` |
| Images | `zqr:dev`, `zqr-agent:dev` (built by `zqr desk setup`) | `ZQR_IMAGE`, `ZQR_AGENT_IMAGE` set by the host wrapper |
| Sibling mounts (`mount()`) | bind mounts by path | `--mount type=volume,src=zqr-data,volume-subpath=<rel>`; paths outside `ZQR_HOME` are refused |
| Scratch dirs (`scratch_dir()`) | system temp | `ZQR_HOME/tmp` (the daemon can only see the volume) |
| Gateway network | `network` in `desk/config.json` (picked by `desk check`) | `container`: gateway binds 0.0.0.0 in zqr-desk; each run's proxy joins `zqr-control` and forwards to the desk's address |
| Credentials | `~/.config/zqr/desk.env` or shell env | `/run/zqr/desk.env` on volume `zqr-secrets` (read-only in the desk) |

The release container is detected by `ZQR_DATA_VOLUME`. The desk runs as root in its container and hands
workspaces to the agent's uid (1000); the egress proxy and sandbox run as uid 10001.

## 4. Repository layout

| Path | Responsibility |
|---|---|
| `src/quant_research/cli.py` | `zqr` entry point; registers all subcommands (`desk` via `desk/commands.py`, `release`) |
| `paths.py`, `hashing.py`, `timeutil.py` | `ZQR_HOME` layout, content hashing, UTC helpers |
| `runtime.py` | Resources, version, image names, sibling-container mounts, scratch dirs, control network |
| `release.py` | `zqr release`: build, cross-build, ship and start images on a host |
| `data/` | Coinbase REST client (`coinbase.py`), bar normalisation, validation, snapshots, synthetic data |
| `strategy/` | `StrategySpec`, templates (`buy_and_hold`, `cash`, `sma_cross`, `momentum`, `breakout`, and the seed templates `tsmom`, `ma_filter`, `ewma_trend`, `vol_target`), decision cadence (`cadence.py`), agent plugins, sandbox (subprocess or Docker) |
| `library/` | Strategy library: seed catalog (`seeds.yaml`, reviewed in `docs/library/seeds.md`), catalog validation, library tables in `experiments.db`, `zqr library` commands |
| `portfolio/policy.py` | Portfolio policy v1: vol target, asset/gross caps, rebalance cadence, no-trade band |
| `portfolio/` (other modules) | Portfolio layer (section 5.9): versions and lifecycle (`version.py`), blend (`blend.py`), decision step (`decide.py`), backtest (`backtest.py`), dev/validation runs (`evaluate.py`), seed portfolio (`seed.py`), approve/resume (`paper.py`), `zqr portfolio` commands |
| `backtest/engine.py` | Accounting engine (fills, fees, slippage, delay) |
| `backtest/rebalance.py` | `needs_trade`: when a target becomes an order; shared by the engine and the paper trader |
| `evaluation/` | Metrics, statistics (bootstrap, deflated Sharpe), diagnosis; `split_stats.py`: daily-return Sharpe, split DSR at K, registered appraisal test (appendix A4-A5) |
| `memory/` | Research memory (appendix A): daily return archive, market residuals, clustering and effective K, family tags and proposals, seed characterisation, session conclusions, family archives, clustering report, `zqr memory` commands |
| `validation/` | Splits, holdout control (viewing limit), robustness, walk-forward |
| `experiments/` | Experiment gate (`gatekeeper.py`), runner, SQLite store, replay |
| `research/` | Mandates, preregistered plans, candidate registry and transitions, evaluation, standing authorization |
| `controller/` | Research controller/session state, agent runners, budgets |
| `gateway/` | `ToolGateway` service and MCP server (`submit_plan`, `submit_experiment`, `get_experiment`, `read_source`, compare, dataset info, research.json validation) |
| `bench/` | Agent container adapter, egress network/proxy, isolation probe, audit, recovery, benchmark runner/grader |
| `paper/` | Paper trader and portfolio trader, broker simulator, risk limits, orders, ledger (decisions, fills, bars, marks), bar store, fleet, parity |
| `desk/` | Desk config, presets, promotion, research loop, supervisor, daily report and portfolio sections, monthly review, soft stop, setup/check, CLI commands |
| `report/` | Markdown research reports |
| `scripts/zqr-host.sh` | Host wrapper, installed from the image as `~/.local/bin/zqr` on the deployment host |
| `scripts/windows/setup.ps1` | One-time Windows preparation: Docker check, power settings, `zqr.cmd` on PATH |
| `Dockerfile` | Runtime image `zqr:<version>`: `python:3.12-slim`, locked deps (`uv sync --frozen --no-dev`), docker CLI, `benchmark/` without answers; runs the desk, egress proxy and strategy sandbox |
| `Dockerfile.agent` | Agent image `zqr-agent:<hash>`: `node:22-slim` + `@anthropic-ai/claude-code`, runs as `node` |
| `.dockerignore` | Allowlist; `var/`, `benchmark/evaluator_private/` and benchmark results never reach the daemon |
| `benchmark/` | Benchmark tasks, schemas, fixtures; `evaluator_private/` holds answer keys (never mounted for agents, never in an image) |
| `tests/` | pytest suite (303 tests); Docker-dependent tests skip when Docker or the images are unavailable |
| `docs/`, `development-plan*.md`, `AGENTS.md` | Contracts, this guide, design plans, agent instructions |

## 5. Core flows

### 5.1 Research session (`desk/research.py`)

1. `ResearchLoop.step()` checks its gates in this order. Each gate either returns a wait or lets the loop
   continue:
   - research disabled in config;
   - no mandates;
   - `check.json` does not show `isolation_ok`;
   - no model credential;
   - paused or backing off;
   - daily cost cap reached;
   - `max_sessions_per_day` reached.
2. Topic rotation: products × `TOPICS` (`trend`, `mean_reversion`, `volatility_filter`, `multi_timeframe`).
3. The workspace receives `ROLE.md`, `research.schema.json`, `fixtures/strategy_contract.md` and a generated
   `TASK.md` (mandate, budget, earlier candidates).
4. The agent runs `claude -p` with tools `Read Write Edit Glob Grep mcp__quant`. These tools are denied:
   `Bash WebFetch WebSearch Task NotebookEdit`.
5. When the run ends:
   - The workspace is frozen.
   - The audit checks that every experiment has a matching tool event and that egress logged no denials other
     than the model host.
   - Cost comes from priced transcript usage.
   - The outcome is classified.
6. If `research.json` declares a `final_artifact`, the controller:
   - checks it (hashes, cited numbers);
   - registers a candidate;
   - evaluates it on validation with the mandate's acceptance rules;
   - moves it to `validated` or `rejected`;
   - on acceptance, calls `promote`.
7. Every session writes `desk/sessions/<run_id>/result.json` and one line in `desk/research_ledger.jsonl`,
   both with `zqr_version`, `harness` (Claude Code version) and `model`.

Failure handling:

- Environment failures (`environment.model_unreachable`, `environment.docker`, `harness.error`) pause the loop
  with exponential backoff (300 s doubling, up to 3600 s).
- Other failures back off from 60 s, doubling.
- A stop request records the session as `desk.stopped`.
- `recover_desk_runs` closes out sessions left without a `result.json` by a dead process (`harness.crash`).

Only `tool_refused` events whose `policy` flag is true count as violations. Input or schema mistakes do not.

### 5.2 Candidate lifecycle (`research/registry.py`)

```text
draft -> development -> validated -> frozen -> final_evaluated -> paper_approved -> paper_active
   \          \             \           \
    rejected   rejected      rejected    failed_final
```

| Transition | Allowed actors |
|---|---|
| development -> validated | `controller` only (deterministic acceptance) |
| validated -> frozen | `human`, or `desk` with a valid authorization |
| frozen -> final_evaluated / failed_final | `evaluator` (holdout run) |
| final_evaluated -> paper_approved | `human`, or `desk` with a valid authorization |
| paper_approved -> paper_active | `human`, or `paper` (fleet attach hook) |

Desk transitions record the authorization hash in the evidence. No transition to live exists in code.

### 5.3 Standing authorization and promotion

- `research/authorization.py`: an `Authorization` lists products, `max_paper_strategies`, `created_at`,
  `expires_at`, `created_by`, a note and a scope. The scope is the two desk transitions. The body is sealed with a
  hash, and `check()` rejects a bad hash, expiry, a transition outside the scope or a product that is not listed.
- `desk/promotion.py` `promote()` runs these steps and stops at the first refusal, leaving the candidate in the
  state it reached:
  1. Check the authorization against the dataset's product.
  2. Refuse a mandate whose engine settings paper cannot reproduce (`execution_delay_bars` or `adverse_fill`
     other than the defaults, or a `rebalance_tolerance` above the policy band, which paper trades as its
     tolerance).
  3. Check the paper-slot cap (candidate entries in the roster), so a full roster never spends a holdout viewing.
     A `paper_approved` candidate skips this check.
  4. If `validated`: freeze the candidate (actor `desk`).
  5. If `frozen`: run the holdout through the capped gate (`run_candidate_holdout`). Exhausted viewings or a
     failed pass rule stop promotion here.
  6. Check the paper-slot cap again.
  7. Move the candidate to `paper_approved` (actor `desk`).
  8. Add the roster entry `<base>-<cid>` with source `candidate` if it is missing. It carries the mandate's
     policy, `initial_cash`, fee rate, slippage and minimum trade notional.
- Promotion resumes from `validated`, `frozen`, `final_evaluated` or `paper_approved`; a frozen candidate whose
  holdout already ran gets its recorded result back without a new viewing.
- When the fleet attaches that entry, `activate_hook` moves the candidate to `paper_active`.

### 5.4 Paper fleet (`paper/fleet.py`)

- The roster (`desk/roster.json`) holds the default fee rate 0.006 and slippage 5 bps, `RiskLimits` and the
  entries. An entry may set its own `fee_rate`, `slippage_bps` and `min_trade_notional` (promoted candidates
  do). Defaults are created from `desk/presets.json`: 16 entries, 8 rules × 2 products.
- The fleet reloads the roster when its mtime changes:
  - an invalid roster is logged and the running set is kept;
  - removed or disabled entries are detached, and their ledgers are kept;
  - a changed risk section is applied to running traders (`PaperTrader.update_risk`);
  - a change to anything in `PaperConfig.identity_hash()` (strategy, parameters, policy, capital, costs; risk and
    sandbox are excluded) is refused: the trader keeps running its old configuration, the fleet logs
    `config_change_refused` and `fleet_health.json` lists "config change refused" under `errors` until the edit is reverted or the strategy
    is added under a new name;
  - attaching a strategy runs make_trader → recover → warm_up → health. Warm-up loads the bar store up to the
    ledger's `last_bar_open` (the whole store for a new trader), so bars the trader missed while stopped are
    delivered by the next step. A failed attach is retried after min(60·2^(n−1), 1800) s.
- Each product/granularity has one feed: a bar store (`paper/barstore.py`) plus a poller. A new store is seeded
  from the frozen Coinbase dataset for that product and granularity reaching furthest back, else from the latest
  17,520 venue bars (`SEED_BARS`); after that it is only appended. Each poll fetches every completed bar after
  the last stored one, so an outage is backfilled. A bar counts as completed 60 s after its close
  (`SETTLE_SECONDS`), so a candle is never read while still forming. Gaps the venue leaves raise a `bar_gap` alert.
- Every step, each trader receives the stored bars it has not seen (`_catch_up`): all but the latest are ingested
  without deciding (state, ledger bars and marks only), the latest goes through `on_bar`. Errors are isolated per
  trader. When any trader has live orders, the fleet fetches one mid price per product.
- Orders follow the backtest's rebalancing rule (`backtest/rebalance.py` `needs_trade`, including the policy
  band and the minimum trade notional).
- Default risk limits:
  - max position fraction 1.0;
  - max order notional: the preset roster sets it to the strategies' capital ($10,000), so a full entry fills in one
    bar as in the backtest (the `RiskLimits` default is $5,000);
  - daily loss 5%;
  - 6 orders per hour;
  - stale after 7200 s, measured from the latest bar's close (`last_data_time`); the time the bar was fetched is
    kept separately (`last_seen_time`, shown as `last_seen` in health). The limit in force is at least two bar
    intervals (`stale_after_effective` in health), so daily strategies are not stale between bars;
  - optional kill-switch file.
- Each decision records `history_start`, target, price and equity; every bar is stored in the ledger, and the
  trader marks equity at every bar close (ledger table `marks`). `paper/parity.py` `paper_parity` checks, for
  decisions whose `history_start` is the bar store's first bar (older ones are counted as skipped):
  - targets: `target_fraction` recomputed from the bar store with the research code must match exactly;
  - orders: `needs_trade` applied to the marked position and equity at that bar must give the same side (or no
    order);
  - sizes: the order quantity must match the backtest rule (`rule_quantity`) within a cent; an order limited by
    `max_order_notional` (as recorded with the decision; the roster's value for older decisions) or the cash buffer, or resized by the risk limits, is an allowed difference, counted
    separately;
  - fills: average price against the next bar's open ± slippage, and the fee against the configured fee rate;
  - path: `run_backtest` from the paper cash and position at the first checked decision; the equity gap at the last
    one is split into execution (model cost minus actual cost of the fills) and sizing and timing (the rest).
- Outputs: `paper/bars/`, `paper/fleet_health.json`, `paper/fleet.jsonl`, and `paper/<strategy>/` (`paper.db`,
  `events.jsonl`, `health.json`).

### 5.5 Supervisor (`desk/supervisor.py`)

- Its children are `python -m quant_research.cli --home <ZQR_HOME> paper fleet` and
  `... desk research-loop`.
- Restart backoff runs from 5 s to 300 s; the counter resets after 600 s of healthy running.
- Credentials: the research child gets the inherited environment plus the credentials file (`ZQR_CREDENTIALS`,
  default `~/.config/zqr/desk.env`; `/run/zqr/desk.env` in the release container). The fleet
  child has every model variable (`MODEL_ENV`) removed. The research loop also re-reads `desk.env` while it is
  blocked, so a key added later takes effect without a restart.
- Graceful stop: research first (180 s grace), then fleet (30 s). `zqr down` gives the container 300 s.
- Writes `desk/state.json` and logs `desk/logs/{supervisor,fleet,research}.log`.
- Writes the daily report for the previous UTC day at 00:05 UTC.

### 5.6 Daily report (`desk/report.py`)

`desk/reports/<day>.md` contains:

- per-strategy equity, day and total return from the bar-close marks against buy & hold (total buy & hold pays
  the entry fee and slippage), fills, fees, alerts, and parity on targets, orders and fills;
- parity per decision: the target, the order side and the order size re-derived with the backtest rule; orders
  limited by the order cap, the cash buffer or the risk limits are counted as allowed differences, not mismatches;
- a shadow backtest from the paper cash and position at the day's first decision: the equity gap in bps, split into
  execution (fill price and fees against the cost model) and sizing and timing, and the final exposure gap;
- process restarts and fleet alerts;
- research sessions by failure class, cost against the cap, violations and audit failures;
- trials per split, effective K (ρ 0.7, latest clustering), candidates by state and holdout viewings left, flagged
  "(contaminated by override)" when the split has a recorded override, and the earliest date of the next split;
- a table of sessions.

### 5.7 Strategy library (`library/`)

- The seed catalog lists families and template entries with fixed parameters; each entry is instantiated per
  product (`btc-tsmom-12m`, …). Validation refuses unknown templates or parameters, a member in a non-sleeve family
  (or an overlay in a sleeve family), a sleeve family with fewer than two variants, and a member share above
  `max_member_share` (equal sleeves, equal weight within a family).
- Seed templates decide at a calendar cadence on hourly bars (`strategy/cadence.py`): a bar is a boundary when its
  nominal close (open + 1 h) falls in a later UTC day, Monday-based week or month than the previous bar's, so the
  bar closing at 00:00 UTC decides, or the first bar after a gap. The value is held until the next boundary.
  `ewma_trend` and `vol_target` compute on the daily close series of those boundary bars.
- `zqr library import` adds new instances as `candidate` and records an event; existing ids with the same identity
  are left alone, and a changed identity refuses the whole import (entries are immutable; a change is a new id).
  Status changes go through `LibraryStore.set_status` (candidate → probation/member/retired, probation →
  member/retired, member → retired), each recorded in `library_events`.

### 5.8 Research memory (`memory/`)

Rules are in `docs/contracts-v2-appendix-a.md` (approved 2026-10-15).

- **Return archive.** Every completed or duplicate dev trial's daily returns go to
  `library/returns/<split_id>.parquet` (one column per experiment, UTC days of the bar open time, last mark of the
  day, first day against initial cash). `return_index` records each trial, including failed and rejected ones
  (`no_returns`). The runner archives after every trial, including gate rejections; a failure to archive is logged
  and never fails the trial. `zqr memory backfill` fills in older trials.
- **Clustering.** Residuals of each series on the coin's close-to-close return (OLS with intercept), winsorised once
  at mean ± 3 sd. Flat series (max |r| < 1e-12) and passive series (winsorised residual sd < 1e-4, amendment
  A13.1, so buy and hold is passive whatever its entry day costs) form one cluster each.
  Pearson correlation, distance `sqrt((1 − ρ)/2)`, average linkage, cuts at ρ 0.3, 0.5 and 0.7; a cluster's label
  is its smallest experiment id. K = clusters + flat + passive + trials without returns; K(0.7) is the effective
  number of trials. A clustering is recorded only when its input hash (returns, thresholds, passive rule version)
  changes (`clusterings`, `cluster_members`).
- **Families.** Seed runs are tagged with their library family; older trials with their session topic
  (`volatility_filter` → `vol_overlay`, `mean_reversion` → `short_term_reversal`, archive only). A ρ 0.3 cluster
  maps to the family with the most seeds in it; when families tie it maps to all of them as a shared cluster
  (amendment A13.2: in each archive with `is_shared` and `shared_with`; reports also give deduplicated totals).
  Without seeds it maps to a family holding more than half its trials. Unmapped clusters of at least 5 trials
  become `family_proposals` for the human: `zqr memory proposals` lists them, `zqr memory reject <ids> --reason`
  records a rejection in the append-only `proposal_decisions`. A rejected cluster stays unmapped as a negative
  result and is not proposed again while its members are unchanged.
- **Seed characterisation.** `zqr memory characterise` runs each seed instance once on its product's desk mandate
  (kind `seed`, counted as trials, duplicates not reused). `zqr library periods --product P --cut D` gives each
  seed run's statistics per period from its recorded equity (`evaluation/periods.py`).
- **Split lineage** (amendment A13.4). A split that extends an earlier split's dev backwards names it as parent
  (`zqr split-lineage`, human; same product and granularity, equal validation and holdout windows, the child's
  dataset extends the parent's). K at each threshold is the split's own clustering plus its ancestors' latest
  (`clusters.k_breakdown`; `effective_k` returns the total), and the legacy DSR deflates with the trial count of
  the split and its ancestors (`ExperimentStore.split_trial_count`). A split refuses holdout requests when it or
  any ancestor is closed (`closed_refusal`), whichever was recorded first. Reports show K and trials as own +
  inherited.
- **Conclusions.** `zqr memory import-conclusions` imports each session's `research.json` into the append-only
  `family_conclusions` after re-running the gateway's citation check.
- **Archives.** `library/archives/<family>.json` and `.md`: trials, clusters, parameter ranges, the best trials
  with split DSR, cost ×2 reruns, regime splits and conclusions. Agent plugins are rerun only under Docker
  isolation (`--sandbox subprocess` skips them).
- **Report.** `library/reports/clustering-<day>.md`: K per split at the three thresholds (winsorised and raw), the
  cluster size distribution, the largest clusters, seed placement, proposals, the top trials with split DSR beside
  the legacy DSR, and conclusions by topic and decision.
- `zqr memory run` performs all steps in order; each step is idempotent.

### 5.9 Portfolio layer (`portfolio/`, phase 3a)

Rules: `docs/investment-policy.md` (D1-D3) and appendix A14 (D4-D7). Plan:
`docs/superpowers/plans/2026-10-15-phase3-portfolio.md`.

- **Versions.** A `PortfolioVersion` is one product's blend: members (library entries of sleeve families), an
  optional overlay, `member_cap` 0.15, `band` 0.10, the drawdown ladder, the engine config (no delay, no adverse
  fill, no rebalance tolerance) and the dataset and split. Its id is `pv-` + the hash of the body; bodies never
  change. Lifecycle in `portfolio_versions` / `portfolio_events`: draft → validated | failed_validation
  (controller) → frozen (human) → final_evaluated | failed_final (evaluator) → paper (human); any non-terminal
  state → retired (human).
- **Blend.** Equal sleeves over families, equal weight within a family, each member capped at `member_cap` and
  halved on probation; the overlay multiplies the sum; the target is clipped to [0, 1].
- **Decision step** (`decide.py`). The deadband trades only when the target moves at least `band` from the last
  executed target (which starts at 0: the account starts in cash), on a full exit, or when the ladder level
  changes. Ladder on the drawdown from the peak bar-close equity: −20% yellow flag, −25% halved (×0.5), back to
  full at −15%, −30% liquidated and halted until a human resume (which resets the peak). Paper catch-up bars update
  the ladder without trading; a level change seen there trades at the next decision.
- **Backtest.** `run_backtest(..., decider=...)` calls `decide(i, equity, position)` at each decision bar and
  `executed(i, target)` after a fill; it needs delay 0 and uses tolerance 0. `run_portfolio` runs a version and
  logs every decision; the same run with a ladder at −∞ gives the drawdown without the ladder (D4).
- **Runs** (`evaluate.py`). A dev run is an experiment of kind `portfolio` (a trial, archived in the return
  archive); a validation run is kind `portfolio_validation` (not a trial; counted in `validation_uses`). Both
  record daily Sharpe (A4), drawdown with and without the ladder, ladder time, family contribution and buy and
  hold. Each run also writes `equity_no_ladder.parquet` with its Sharpe and return: the series research measures
  read (A13.6: the return archive, gate 1's `R_p`, gate 2), since a backtest never resumes after a liquidation.
  `zqr portfolio validate` applies the seed standard: validation daily Sharpe ≥ 0.5 and max drawdown ≥ −25%.
  A failed version is recorded, not adjusted and retried (A6). `zqr portfolio dev <vid> [--reference]` runs dev
  alone (no validation use, state unchanged); `--reference` records the version as gate 1's reference for its
  product and split (D4, one per product and split). `zqr portfolio periods <vid> --cut D` splits the dev run's
  statistics by period, with buy and hold and ladder time, and again without the ladder.
- **Seed portfolio** (`seed.py`). Every non-retired library member of the product's sleeve families plus the
  `vol_overlay` entry, with the desk mandate's engine config (rebalance tolerance 0), dataset and split.
- **Holdout.** `run_portfolio_holdout` (`validation/holdout.py`) needs a frozen version, shares the split's viewing
  cap with candidates, allows at most one portfolio version per split every 7 days (A9), and applies gate 3 (daily
  Sharpe ≥ 0.5 and a positive return). A repeat returns the recorded result without a new viewing;
  `--override-reason` lifts a closure (A13.3), the cap or the 7-day rule and marks the split's holdout as
  contaminated.
- **Paper.** `zqr portfolio approve` (final_evaluated → paper) adds the roster entry `portfolio-<product>`
  (`source "portfolio"`, `portfolio_version`) and moves the version's candidate library entries to member. The
  fleet loads the version body into `PaperConfig.portfolio` (part of the identity hash, so changing the version is
  refused) and runs a `PortfolioTrader`: the blend on the trader's history, the same decider, order cap = capital
  (D1), zero tolerance. Its state is the ledger key `portfolio_state`; every bar gets a decision record with a
  `portfolio` block (blend, family targets, level, `pre_state`, order target). `zqr portfolio resume <name>
  --note` writes `paper/<name>/resume.json`, applied once at the next bar and logged as an ALERT. Portfolio
  entries do not count against the candidate paper slots.
- **Shadow portfolios** (phase 4, D2). `zqr portfolio shadow <vid> --note` (human) runs a version in `validated`,
  `failed_validation` or `failed_final` as the roster entry `shadow-<product>` (`shadow: true`), the same
  `PortfolioTrader` with the version's capital and costs, for forward evidence only. The version's state and the
  library statuses do not change; the start is a version event with from_state = to_state. The `shadow` flag is not
  part of the identity hash. Reports show shadows in their own table, labelled not approved; they are excluded
  from the combined account and the soft stop.
- **Parity** (`portfolio_parity`). Per decision: blend recomputed from the bar store, decider re-run from the
  recorded `pre_state` with the marked equity and position, order side and size; path: `run_portfolio` from the
  first decision's account and state.
- **Reports.** The daily report has a portfolio section (equity, day and total return against buy and hold with
  the same capital and costs, drawdown, ladder level, composition, family P&L for the day, fills, turnover, fees,
  parity) and the combined account (sum of the products, yellow flag on its drawdown). The research table adds
  portfolio versions by state and validation uses per split. On the last day of a month the report job also writes
  `desk/reports/monthly-YYYY-MM.md` (`zqr desk monthly --month`).
- **Soft stop** (D2, `desk/softstop.py`). On the monthly review, once the combined account has 365 days of daily
  returns: trailing-365-day Sharpe minus buy and hold's < −0.5 writes `desk/soft_stop.json` and the research loop
  pauses (paper continues). `zqr desk soft-stop clear --note` (human) keeps the record as
  `soft_stop.cleared-<ts>.json`.

## 6. Security model and invariants

These must hold after any change. Tests cover most of them.

1. The agent cannot reach anything except the model host and the gateway. The isolation probe
   (`bench/isolation.py`, run by `zqr desk check`) must pass before research starts.
2. The agent's tools see `dev` only. Validation and holdout are evaluated by the controller and evaluator.
   `benchmark/evaluator_private` and `var/controls` are never mounted, and never enter the runtime image
   (agent-authored strategy code runs in that image; `tests/test_runtime.py` guards `.dockerignore`).
3. Model credentials go only to the research child and are passed by name into the agent container. The paper
   fleet never receives them. The credential file is mode 600 and is never printed or logged.
4. Only the deterministic controller sets `validated`. Desk transitions require a valid sealed authorization.
   Nothing transitions to live.
5. Every experiment, tool call, transition and paper decision is recorded. Session audits must reconcile
   experiments with tool events.
6. Trials on a split are never deleted. DSR counts all of them, and those of the split's ancestors (A13.4). Datasets
   are immutable; an extended dataset copies its base's bars verbatim (`extend_dataset` checks the base's content
   hash). Holdout data is read only through
   `validation/holdout.py` (`run_holdout` for candidates, `run_portfolio_holdout` for portfolio versions), which
   caps viewings per split and refuses closed splits (A13.3); exceeding the cap or viewing a closed split needs a
   recorded override and marks the split's holdout as contaminated. A test enforces that no other module reads it. Experiment replay refuses holdout runs and reads
   the split's research segment; `zqr paper replay` stops at the earliest protected holdout start of the product
   and granularity (`holdout_cutoff`: a holdout a later split folds into its research data is no longer protected).
7. No trading credentials exist. Paper fills are simulated against public market data.

## 7. Runtime layout (`ZQR_HOME`)

| Path | Content |
|---|---|
| `research.sqlite` | Experiments, plans, sessions, events, candidates, mandates |
| `raw/`, `snapshots/`, `splits/`, `holdout/` | Imported data, frozen datasets, split definitions, holdout segments |
| `artifacts/` | Frozen candidate code and identity |
| `sessions/` | Controller session workspaces |
| `paper/` | Bar stores (`bars/<product>-<granularity>.parquet`), fleet health/log and one directory per paper strategy or portfolio (`portfolio-<product>/`, with `resume.json` while a resume is pending) |
| `desk/config.json` | `DeskConfig` (below) |
| `desk/mandate_history.jsonl` | Desk mandate switches (`zqr desk use-mandate`): product, from, to, split, note |
| `desk/check.json` | Last `desk check` result, including `isolation_ok` and the chosen network mode |
| `desk/authorization.json` | Standing authorization |
| `desk/roster.json` | Paper roster |
| `desk/research_state.json`, `desk/research_ledger.jsonl` | Loop state, one line per session |
| `desk/sessions/<run_id>/` | `run.json`, `result.json`, submission, egress log, agent transcript |
| `desk/state.json`, `desk/logs/`, `desk/reports/` | Supervisor state, logs, daily reports and monthly reviews (`monthly-YYYY-MM.md`) |
| `desk/soft_stop.json`, `desk/soft_stop.cleared-*.json` | Active soft stop (research paused); cleared ones with the human's note |
| `library/returns/`, `library/archives/`, `library/reports/` | Daily return archive per split, family archives, clustering reports |
| `tmp/` | Release container only: scratch dirs mounted into sibling containers |

The credentials file sits outside `ZQR_HOME` (a separate volume in the release layout) and holds the model
variables for the research child only.

## 8. Configuration

The fields of `DeskConfig` (`desk/config.json`), with defaults:

| Field | Default | Meaning |
|---|---|---|
| `products` | BTC-USD, ETH-USD | Products researched and traded |
| `mandates` | set by setup | Product → mandate id |
| `daily_cost_cap_usd` | 20 | No new sessions after this spend (UTC day) |
| `max_sessions_per_day` | 200 | Session cap per UTC day |
| `session_wall_seconds` | 1800 | Agent wall clock per session |
| `backoff_base_seconds` / `backoff_max_seconds` | 300 / 3600 | Environment-failure pause |
| `poll_seconds` | 30 | Fleet poll interval |
| `report_hour_utc` / `report_minute_utc` | 0 / 5 | Daily report time |
| `model` | none | Passes `--model` to Claude Code. When unset, `ANTHROPIC_MODEL` applies |
| `agent_memory` / `agent_cpus` | 2g / 2 | Agent container limits |
| `research_enabled` | true | Master switch for research |
| `network` | docker_desktop, 127.0.0.1, host.docker.internal | Development only: gateway bind and proxy forward host, picked by `desk check`. The release container always uses `container` mode, computed at run time |

Desk mandate defaults (`desk/setup.py`):

- **Acceptance:** Sharpe ≥ 1.0, DSR ≥ 0.9, at least 10 round trips, max drawdown not worse than −25%,
  profitable with 2× costs and with a one-bar delay.
- **Holdout:** at most 10 viewings per split; a pass needs Sharpe ≥ 0.5 and a return > 0.
- **Budget:** 12 dev experiments per session.
- **Splits:** 60/20/20 by time.

Model: the agent harness is Claude Code (pinned in `Dockerfile.agent`; currently 2.1.112). `ANTHROPIC_BASE_URL`
selects the provider through an Anthropic-compatible API. The current provider is DeepSeek (`deepseek-flash`).
Changing provider changes the egress allowlist host, so re-run `zqr desk check`.

## 9. Commands

On the deployment host, `zqr` is the host wrapper (`scripts/zqr-host.sh`); on Windows, `zqr.cmd` forwards to it
inside WSL. Wrapper commands manage the container; every other command runs `zqr ...` inside `zqr-desk` (or in
a one-off container with the same mounts while the desk is stopped).

| Host command | Purpose |
|---|---|
| `zqr up [VERSION]` | Start the desk, or switch it to VERSION (stops the old container gracefully first) |
| `zqr down` / `zqr restart` | Graceful stop (stays stopped across reboots) / stop and start |
| `zqr status` | Same as `zqr desk status` |
| `zqr versions` / `zqr rollback` / `zqr prune` | Images on the host / switch to the previous version / delete all but current and previous |
| `zqr secrets set\|import\|status` | Model credentials into volume `zqr-secrets`: hidden input / `KEY=VALUE` lines on stdin / names only |
| `zqr setup [ARGS]` | `zqr desk setup` in a one-off container (data, mandates, roster, authorization) |
| `zqr logs [-f]`, `zqr shell` | Container output, a shell inside the desk |

| Desk command | Purpose |
|---|---|
| `zqr desk setup [--skip-images] [--rekey] [--yes]` | Images (development: builds `zqr:dev`), data, mandates, roster, credentials, authorization |
| `zqr desk check [--json]` | Docker, images, Coinbase clock skew, disk, credentials (names only), model endpoint, mandates, authorization, isolation probe |
| `zqr desk status [--json]` | Version, supervisor, children, research state and spend, fleet health, authorization, check |
| `zqr desk logs [supervisor\|fleet\|research\|events] [-f]` | Component logs |
| `zqr desk report [--day YYYY-MM-DD] [--print]` | Write or print the daily report |
| `zqr desk authorize [--renew\|--revoke]` | Show, renew or revoke the standing authorization |
| `zqr desk secrets [--stdin\|--status]` | Write the credentials file (what `zqr secrets` runs) |
| `zqr desk up` / `down` | Development checkout only: background supervisor. Refused inside the release container |
| `zqr desk research-loop`, `zqr paper fleet` | Supervisor children (foreground, for debugging) |
| `zqr desk recover` | Close out sessions of a dead desk process |
| `zqr desk promote <candidate_id> [--isolation docker\|subprocess]` | Resume a candidate's promotion (freeze, holdout, paper approval, roster) under the standing authorization |
| `zqr library import [--dry-run] [--file PATH]` | Import the reviewed seed catalog into the library (new entries only) |
| `zqr library list [--product P] [--family F] [--status S] [--json]` / `zqr library show <id>` | Library entries; one entry with its events and its seed run (dev Sharpe, split DSR) |
| `zqr memory backfill\|characterise\|cluster\|import-conclusions\|archive\|report` / `zqr memory run [--sandbox docker\|subprocess]` / `zqr memory status` | Research memory steps (section 5.8); all steps in order; archived trials and K per split |
| `zqr memory proposals [--status open\|rejected\|all]` / `zqr memory reject <ids> --reason` | Family proposals; record a human rejection |
| `zqr portfolio seed --product P` / `validate <vid>` | Define the seed portfolio (draft) / dev and validation runs and the seed standard (controller) |
| `zqr portfolio list [--product P] [--state S]` / `show <vid>` | Versions; one version with shares, evidence and events |
| `zqr portfolio freeze <vid> --note` / `holdout <vid> [--override-reason R]` | Human freeze / holdout test (gate 3, spends a viewing) |
| `zqr portfolio approve <vid> --note` / `resume <name> --note` | Human: paper roster entry / resume a liquidated paper portfolio |
| `zqr portfolio shadow <vid> --note` | Human: run a version not approved for paper as `shadow-<product>` (forward evidence only) |
| `zqr close-holdout --split S --note` | Human: close a split's holdout to new viewings, permanently (A13.3) |
| `zqr extend-dataset --dataset D --start T [--fill-policy ffill_flat]` | Freeze earlier candles (fetched first with `zqr fetch-data`) followed by D's bars, copied verbatim; isolated bad prints are dropped and listed (A13.5, as in every research freeze) |
| `zqr create-split --dataset D --like S` / `zqr split-lineage --split C --parent S --note` | A split with S's validation and holdout windows / human: name its parent (A13.4: K and trials inherited, closure inherited) |
| `zqr desk use-mandate --product P --mandate M --note` | Human: point a desk product at another stored mandate (research disabled; history in `desk/mandate_history.jsonl`) |
| `zqr portfolio dev <vid> [--reference]` / `periods <vid> --cut D` / `zqr library periods --product P --cut D` | Dev run alone, optionally gate 1's reference / per-period statistics of recorded dev runs |
| `zqr desk monthly --month YYYY-MM` / `zqr desk soft-stop status\|evaluate\|clear [--day D] [--note N]` | Monthly review; soft-stop rule (investment policy 2) |
| `zqr release [--target HOST\|local] [--allow-dirty] [--no-up]` | Development machine: build, ship and start a version |

Lower-level commands (`fetch-data`, `backtest`, `mandate`, `candidate`, `holdout-test`, `bench`, `paper`, …)
are listed in `zqr --help`.

## 10. Deployment

### Release flow

`zqr release` (default target `quant-wsl`) runs on the Mac from a clean checkout:

1. Version `<pyproject version>-<commit>`; uncommitted changes to image inputs are refused unless
   `--allow-dirty` (the version then gets a `-dirty<timestamp>` suffix).
2. Builds `zqr-agent:<hash of Dockerfile.agent>` and `zqr:<version>` for the target's platform (`linux/amd64`
   for the PC, cross-built on the arm64 Mac with buildx). The runtime image records its agent image in the label
   `zqr.agent`.
3. Ships each image the target does not have: `docker save | gzip | ssh TARGET 'gunzip | docker load'`. The agent
   image only moves when `Dockerfile.agent` changes; the runtime image (about 870 MB, mostly pandas/pyarrow)
   moves on every release.
4. Installs the host wrapper from the new image as `~/.local/bin/zqr` and runs `zqr up <version>`, which records
   the current and previous version under `~/.local/state/zqr/`.

Rollback: `zqr rollback` on the host. Data in `zqr-data` is shared by all versions, so a release that changes the
database schema or file formats must stay readable by the previous version or say so in its commit.

### Windows PC (production)

The machine has a 12700K and 32 GB RAM, runs WSL2 Ubuntu-24.04 (NAT networking) and Docker Desktop with WSL
integration and auto-start. It is reachable from the Mac as `ssh quant-wsl` (Tailscale address of the WSL
instance).

- The desk is the container `zqr-desk` with `--restart unless-stopped`; Docker Desktop starts it after sign-in.
  Containers run in Docker Desktop's own VM, so the Ubuntu distro may idle without stopping the desk.
- Docker objects: volumes `zqr-data` (all research data) and `zqr-secrets` (`desk.env`), network `zqr-control`.
- `zqr` inside WSL: `~/.local/bin/zqr`, linked as `/usr/local/bin/zqr`. Windows: `%LOCALAPPDATA%\ZQR\bin\zqr.cmd`
  on the user PATH.
- Sleep and hibernate timeouts on AC power are 0. `powercfg /hibernate off` is not applied yet (needs an
  elevated shell: re-run `setup.ps1` as administrator).
- The pre-container installation (source checkout, `var/` with its research history from the first deployment
  day) is archived at `~/Quant-legacy` inside WSL; the release deployment started with fresh data and mandates
  on 2026-10-13.
- Fresh machine: `scripts/windows/setup.ps1` (Docker check, power settings, `zqr.cmd`), then from the Mac
  `zqr release`, then on the host `zqr secrets set` (or pipe them with `zqr secrets import`), `zqr setup --yes`,
  `zqr desk check`, `zqr up`.

### Mac (development)

Run `uv sync`, then use `.venv/bin/zqr` with `ZQR_HOME=var` (default). `zqr desk setup` builds `zqr:dev` and
`zqr-agent:dev`; `zqr desk up` runs the supervisor as a background process. Model credentials on the Mac come from
the shell environment (`zsh -ic`) or `~/.config/zqr/desk.env`.

Release rehearsal on the Mac: `zqr release --target local --allow-dirty --no-up`, then use
`scripts/zqr-host.sh` as the host wrapper (`secrets import`, `setup --yes`, `desk check`, `up`, `down`). This runs
exactly the production layout (volumes, control network, sibling containers) on the local Docker.

## 11. Development workflow

- Toolchain: Python 3.12, uv, pydantic v2, pandas/NumPy, SQLite, `mcp` + uvicorn for the gateway.
- Lint: `.venv/bin/ruff check src tests`, with a line length of 120.
- Tests: `.venv/bin/pytest -q -p no:logging`. Docker tests (`test_egress.py`, `test_sandbox_docker.py`) need
  Docker and the images `zqr:dev` and `zqr-agent:dev`.
- Tests use fakes for Coinbase (`FakeCoinbase`), the agent (`FakeAgent` drives the real `ToolGateway`) and the
  clock. Add a test with every behaviour change; the desk modules have a test file each (`test_fleet.py`,
  `test_desk.py`, `test_supervisor.py`, `test_report.py`, `test_runtime.py`).
- Conventions:
  - Deterministic code owns every decision.
  - New actors or transitions go through `TRANSITIONS`.
  - Anything an agent influences is audited.
  - Credentials never appear in logs, results or command output.
  - Code never locates files relative to the repository: resources come from `runtime.root()`, data from
    `ZQR_HOME`, and anything a sibling container mounts goes through `runtime.mount()`.
- Before releasing: ruff, the full suite, and for desk or deployment changes a local rehearsal (`release --target
  local`, at least one research session and one hourly bar, `zqr desk report --print` with every parity check OK,
  `zqr down`, and no `qr-` containers left). Then commit, push to origin, `zqr release`.

## 12. Known limitations and next steps

- **Research outcome so far:** about 26 sessions and no candidate has passed. The agent's rejections looked
  sound: the best dev Sharpe was about 1.0 with drawdowns past −40% and DSR 0.05–0.08. Hourly long/cash with a
  1.3% round trip is a hard target.
- **Trial accumulation:** the loop adds about 100 trials per split per hour, and DSR deflation grows with them.
  If no candidate appears after 1–2 days, consider fewer sessions per day, new topics or fresh splits.
- **Model and harness attribution:** each session records `zqr_version`, `harness` and `model`, but the daily
  report does not yet break results down by them. Model comparisons (for example DeepSeek pro against flash)
  should use the benchmark controls, not desk outcomes.
- **Benchmarks in the release image:** `evaluator_private/` is not in the image, so `zqr bench` grading runs only
  in a development checkout.
- **Release transfer:** every release ships the whole runtime image (about 250 MB compressed). A private registry
  would ship only changed layers if this becomes slow.
- **Benchmark difficulty:** the v0.2 tasks saturate. Harder tasks are needed before agents can be ranked.
- **Review probes:** `docs/reviews/2026-10-13/reproduce_findings.py` asserts the paper, promotion and holdout
  defects as they were at `a28d9b2`; on the current code it stops at its first assertion (the band defect is
  fixed). It is kept as a record of the review, not as a test. The corresponding
  correct-behaviour tests live in `tests/` (`test_paper.py`, `test_paper_data.py`, `test_fleet.py`,
  `test_desk.py`, `test_experiments.py`, `test_report.py`).
- **Paper history before the bar store:** decisions recorded before a strategy's ledger started from the bar
  store have a different `history_start` and are counted as skipped by the parity check, not verified.
- **Paper buy sizing change:** up to release `0.1.0-b28b1a2c`, paper buys were sized at `delta / close`, about one
  slippage (5 bps) larger than the backtest. A report day that includes decisions from before that release can show
  them as size mismatches.
- **Not built:** M6 multi-asset portfolio, a second agent adapter (Codex), live trading (out of scope by design),
  reconciliation against a real account.
- **Operations:**
  - A Windows Update reboot needs a sign-in before the desk resumes.
  - The standing authorization expires after 30 days (renew with `zqr desk authorize --renew`).
  - TLS interception on a network can cause transient `feed_error` alerts; the feed retries.
