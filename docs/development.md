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
| M6 Portfolio research | **not started** | Multi-asset aggregation; today each strategy trades one product independently |
| M7 Paper (full) | partial | Decision parity, stale-data halt, restart recovery are in; reconciliation and drills beyond tests are not |

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
           ├─ one Coinbase candle poller per product/granularity, one PaperTrader per strategy
           └─ simulated fills (mid + slippage + fees), risk limits, per-strategy ledger, health file
```

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
| `strategy/` | `StrategySpec`, templates (`buy_and_hold`, `cash`, `sma_cross`, `momentum`, `breakout`), agent plugins, sandbox (subprocess or Docker) |
| `portfolio/policy.py` | Portfolio policy v1: vol target, asset/gross caps, rebalance cadence, no-trade band |
| `backtest/engine.py` | Accounting engine (fills, fees, slippage, delay) |
| `evaluation/` | Metrics, statistics (bootstrap, deflated Sharpe), diagnosis |
| `validation/` | Splits, holdout control (viewing limit), robustness, walk-forward |
| `experiments/` | Experiment gate (`gatekeeper.py`), runner, SQLite store, replay |
| `research/` | Mandates, preregistered plans, candidate registry and transitions, evaluation, standing authorization |
| `controller/` | Research controller/session state, agent runners, budgets |
| `gateway/` | `ToolGateway` service and MCP server (`submit_plan`, `submit_experiment`, `get_experiment`, `read_source`, compare, dataset info, research.json validation) |
| `bench/` | Agent container adapter, egress network/proxy, isolation probe, audit, recovery, benchmark runner/grader |
| `paper/` | Paper trader, broker simulator, risk limits, orders, ledger (decisions, fills, bars), fleet |
| `desk/` | Desk config, presets, promotion, research loop, supervisor, daily report, setup/check, CLI commands |
| `report/` | Markdown research reports |
| `scripts/zqr-host.sh` | Host wrapper, installed from the image as `~/.local/bin/zqr` on the deployment host |
| `scripts/windows/setup.ps1` | One-time Windows preparation: Docker check, power settings, `zqr.cmd` on PATH |
| `Dockerfile` | Runtime image `zqr:<version>`: `python:3.12-slim`, locked deps (`uv sync --frozen --no-dev`), docker CLI, `benchmark/` without answers; runs the desk, egress proxy and strategy sandbox |
| `Dockerfile.agent` | Agent image `zqr-agent:<hash>`: `node:22-slim` + `@anthropic-ai/claude-code`, runs as `node` |
| `.dockerignore` | Allowlist; `var/`, `benchmark/evaluator_private/` and benchmark results never reach the daemon |
| `benchmark/` | Benchmark tasks, schemas, fixtures; `evaluator_private/` holds answer keys (never mounted for agents, never in an image) |
| `tests/` | pytest suite (121 tests); Docker-dependent tests skip when Docker or the images are unavailable |
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
- `desk/promotion.py` `promote()` runs these steps:
  1. Check the authorization against the dataset's product.
  2. Freeze the candidate (actor `desk`).
  3. Run the holdout. Exhausted viewings or a failure stop promotion here.
  4. Check the paper-slot cap.
  5. Move the candidate to `paper_approved` (actor `desk`).
  6. Add a roster entry `<base>-<cid>` with source `candidate`.
- When the fleet attaches that entry, `activate_hook` moves the candidate to `paper_active`.

### 5.4 Paper fleet (`paper/fleet.py`)

- The roster (`desk/roster.json`) holds fee rate 0.006, slippage 5 bps, `RiskLimits` and the entries. Defaults
  are created from `desk/presets.json`: 16 entries, 8 rules × 2 products.
- The fleet reloads the roster when its mtime changes:
  - an invalid roster is logged and the running set is kept;
  - removed or disabled entries are detached, and their ledgers are kept;
  - attaching a strategy runs make_trader → recover → warm_up (1000 bars) → health.
- Each feed polls Coinbase candles. Every trader's `on_bar` runs with errors isolated per trader. When any trader
  has live orders, the fleet fetches one mid price per product.
- Default risk limits:
  - max position fraction 1.0;
  - max order notional $5,000 (a $10k position therefore builds over two bars);
  - daily loss 5%;
  - 6 orders per hour;
  - stale after 7200 s;
  - optional kill-switch file.
- Each decision records `history_start`, target, price and equity, and every bar is stored in the ledger. The
  report recomputes `target_fraction` from the ledger bars in `[history_start, bar_open]` and requires an exact
  match (decision parity).
- Outputs: `paper/fleet_health.json`, `paper/fleet.jsonl`, and `paper/<strategy>/` (`paper.db`, `events.jsonl`,
  `health.json`).

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

- per-strategy equity, day and total return against buy & hold over the same decision bars, fills, fees, alerts
  and parity;
- process restarts and fleet alerts;
- research sessions by failure class, cost against the cap, violations and audit failures;
- trials per split, candidates by state and holdout viewings left;
- a table of sessions.

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
6. Trials on a split are never deleted. DSR counts all of them, and holdout viewings are capped per split.
7. No trading credentials exist. Paper fills are simulated against public market data.

## 7. Runtime layout (`ZQR_HOME`)

| Path | Content |
|---|---|
| `research.sqlite` | Experiments, plans, sessions, events, candidates, mandates |
| `raw/`, `snapshots/`, `splits/`, `holdout/` | Imported data, frozen datasets, split definitions, holdout segments |
| `artifacts/` | Frozen candidate code and identity |
| `sessions/` | Controller session workspaces |
| `paper/` | Fleet health/log and one directory per paper strategy |
| `desk/config.json` | `DeskConfig` (below) |
| `desk/check.json` | Last `desk check` result, including `isolation_ok` and the chosen network mode |
| `desk/authorization.json` | Standing authorization |
| `desk/roster.json` | Paper roster |
| `desk/research_state.json`, `desk/research_ledger.jsonl` | Loop state, one line per session |
| `desk/sessions/<run_id>/` | `run.json`, `result.json`, submission, egress log, agent transcript |
| `desk/state.json`, `desk/logs/`, `desk/reports/` | Supervisor state, logs, daily reports |
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
- Sleep and hibernate timeouts on AC power are 0.
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
- **Not built:** M6 multi-asset portfolio, a second agent adapter (Codex), live trading (out of scope by design),
  reconciliation against a real account.
- **Operations:**
  - A Windows Update reboot needs a sign-in before the desk resumes.
  - The standing authorization expires after 30 days (renew with `zqr desk authorize --renew`).
  - TLS interception on a network can cause transient `feed_error` alerts; the feed retries.
