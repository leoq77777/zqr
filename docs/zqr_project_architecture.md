# ZQR Architecture

A short guide to what this project is and how the pieces fit together.
For implementation detail, see [`development.md`](development.md). For frozen interfaces, see [`contracts-v2.md`](contracts-v2.md). For capital and risk rules, see [`investment-policy.md`](investment-policy.md).

## 1. What this is

**ZQR (Quant Research desk)** is a personal, systematic research and paper-trading system for Coinbase **BTC-USD** and **ETH-USD** spot. It trades **long or cash only** on **hourly bars**. There is no leverage, shorting, derivatives, or live exchange credentials.

An AI agent can run experiments around the clock, but it never decides what is true or what gets traded. Deterministic code owns validation, statistics, portfolio construction, risk limits, and paper execution. The agent only proposes ideas and code inside a sandbox; humans approve research campaigns and any path toward real money (live trading is out of scope and not implemented).

In one sentence: **ZQR is a research desk that turns AI-assisted strategy search into auditable experiments, gated portfolio versions, and paper accounts—with hard budget, isolation, and statistical controls.**

## 2. What this is not

| Not this | Instead |
|---|---|
| A hedge fund or production OMS | A single-operator desk with paper capital ($20k total) |
| HFT / order-book microstructure | Hourly OHLCV, next-bar open fills + fees/slippage |
| “The model said so → trade” | Model opinion never changes candidate or portfolio state |
| Unbounded LLM spend | Reservations, daily/monthly caps, priced usage, budget stops |
| Shared multi-asset cash | Two independent product accounts; reports combine them 50/50 |

## 3. Design principles

1. **Determinism over narrative.** Acceptance, gates, costs, and fills are code paths with tests—not chat conclusions.
2. **Dev / validation / holdout separation.** Agents see **dev only**. Validation and holdout are controlled and rate-limited; holdout can be permanently closed.
3. **Blind Gate 2 (A6).** After registration, agents learn that Gate 2 passed or failed—not which check failed or any validation numbers. That blocks adaptive overfitting on the validation set.
4. **Campaigns, not free-form loops.** Research runs as human-approved campaigns with dollar, session, trial, and family quotas.
5. **Same decision function in backtest and paper.** Portfolio deadband, drawdown ladder, and rebalance rules are shared; parity checks compare paper to the research engine.
6. **Ship images, not source, to the host.** Develop on a Mac; deploy versioned Docker images to WSL. Data lives in a volume; secrets stay off the agent containers.

## 4. System at a glance

```text
                    ┌─────────────────────────────────────────┐
                    │  Mac (checkout)                         │
                    │  zqr release → build + ship images      │
                    └──────────────────┬──────────────────────┘
                                       │ docker save | ssh
                                       ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Host (WSL + Docker): container zqr-desk  (image zqr:<version>)          │
│  volumes: zqr-data (/zqr), zqr-secrets (model keys, research child only) │
│                                                                          │
│  DeskSupervisor                                                          │
│    ├─ research-loop   ← model credentials                                │
│    │     per session: agent container (Claude Code)                      │
│    │                  + egress proxy (model host only)                   │
│    │                  + ToolGateway (MCP → experiments on dev)           │
│    │                  + strategy sandbox (agent plugins, no network)     │
│    │                  → freeze, audit, book cost, settle campaign gates  │
│    └─ paper fleet     ← no model credentials                             │
│          bar store + poller per product                                  │
│          PaperTrader / PortfolioTrader per roster entry                  │
│          simulated fills, risk, ledger, health, daily report             │
└──────────────────────────────────────────────────────────────────────────┘
```

Two processes share one data home (`ZQR_HOME`: `./var` in development, `/zqr` in release). Only the research child may call a model. The fleet never sees API keys.

## 5. How research is organized

Research is layered so that “an idea,” “a testable claim,” and “something that can run as a portfolio” are different objects.

```text
Human text / generator
        │
        ▼
   Campaign (proposed → queued → running → concluded | exhausted | cancelled)
        │  sessions under $ / session / trial / family caps
        ▼
   Agent sessions (dev experiments via gateway)
        │
        ▼
   Registered hypothesis (immutable body + variants)
        │
        ├─ Gate 1: marginal value vs reference portfolio on DEV
        └─ Gate 2: no harm on VALIDATION (blind to the agent)
                │
                ▼
         admitted portfolio version  →  waits for next data split
                │
                ▼
         human approve → paper / shadow roster
```

| Layer | Role |
|---|---|
| **Campaign** | Human-approved work unit: product, strategy family, goal, caps. Generator proposes from dev diagnostics; human approves. |
| **Session** | One isolated agent run. Tools go through the gateway; workspace is frozen and audited; cost is booked against a reservation. |
| **Hypothesis** | Frozen claim + variants. Registration spends family quota. Gates run outside the agent’s tool surface. |
| **Portfolio version** | Blend of library members + overlays (deadband, vol target, drawdown ladder). Dev/validation runs and paper share one decision step. |
| **Library / memory** | Seed strategies, family archives, clustering (effective K), conclusions—so failed research becomes reusable evidence. |

**Gate 1** asks whether a variant adds value on development data against a fixed reference portfolio.  
**Gate 2** asks whether the admitted candidate does not hurt Sharpe / drawdown / turnover on validation—without telling the agent which check failed.

Campaign generation must not turn “which Gate 2 metric failed?” or “retry this parameter through Gate 2” into new autonomous work: those are human-review topics or validation snooping, not sandbox tasks.

## 6. How paper trading is organized

```text
desk/roster.json
      │
      ▼
Paper fleet
  • presets (simple controls) + shadow portfolios + approved portfolios
  • one append-only bar store per product/granularity
  • per entry: ledger (decisions, orders, fills, marks), health.json
      │
      ▼
Daily report (00:05 UTC): equity, B&H, parity, research cost, campaigns
```

- **Presets** are fixed simple strategies used as controls.  
- **Shadow** portfolios observe validated / failed versions without counting as the formal account.  
- **Approved** portfolios are the investment path after human approval.  
- Risk: position and order caps, daily loss, staleness, optional kill file; portfolios add a **drawdown ladder** (−20% flag, −25% halve, −30% liquidate + halt). Resume needs cooldown and a written review.  
- **Soft stop:** after enough paper history, if the combined account’s trailing Sharpe is far below 50/50 B&H, research dispatch pauses for human review.

Live trading is not wired. There is no path from paper state to exchange orders.

## 7. Data, splits, and evidence

| Segment | Who uses it | Purpose |
|---|---|---|
| **Dev** | Agent + Gate 1 + research memory | Search, appraisal, family archives |
| **Validation** | Gate 2 only (controller) | Independent check; agents get pass/fail only |
| **Holdout** | Human / capped holdout runner | Final peeks are scarce; splits can be closed forever |

Datasets are immutable snapshots with manifests (hash, fill policy, print corrections). A later cleaning rule can **revalidate** a parent split (same rows, new rule) and inherit trial counts, K, and holdout closure (appendix A13.8).

Cash earns 0 in measurement. Default costs (assumption, labelled): **`coinbase-lf-allin-v1`**
(0.20% + 2 bps ≈ 0.44% round trip); worst-case retail taker contrast remains available
(`coinbase-retail-taker-v0`, ≈ 1.30% RT). See investment policy §6. Research model spend is capped
relative to paper capital.

## 8. Repository map

Package root: `src/quant_research/`.

| Area | Package / path | Responsibility |
|---|---|---|
| CLI & runtime | `cli.py`, `runtime.py`, `release.py`, `paths.py` | Commands, checkout vs image layout, ship to host |
| Market data | `data/` | Coinbase import, validation, snapshots |
| Strategies | `strategy/` | Specs, templates, plugins, sandbox |
| Experiments | `experiments/`, `research/` | Store, runner, mandates, older candidate registry |
| Agent boundary | `gateway/`, `bench/`, `controller/` | MCP tools, isolation, egress, budgets, harness |
| Campaigns | `campaign/`, `hypothesis/`, `inbox/` | Queue, gates, external text → proposed campaigns |
| Memory | `memory/`, `library/` | Archives, clustering, seeds, conclusions |
| Portfolios | `portfolio/`, `backtest/`, `evaluation/` | Blend, decide, engine, metrics |
| Paper desk | `paper/`, `desk/` | Fleet, trader, supervisor, report, soft stop |
| Docs | `docs/` | Contracts, policy, this file, plans, human-only reviews |
| Tests | `tests/` | Behaviour and regression suite |

**Docs worth reading next**

| Doc | When |
|---|---|
| [`development.md`](development.md) | How to run, deploy, and operate day to day |
| [`contracts-v2.md`](contracts-v2.md) + [appendix A](contracts-v2-appendix-a.md) | Timestamps, fills, exposure, statistical rules |
| [`investment-policy.md`](investment-policy.md) | Capital, ladder, research budget, stop rules |
| `docs/superpowers/plans/` | Phase plans (portfolio layer, campaigns) |
| `docs/reviews/` | Human review artifacts—not agent research context |

## 9. Trust and isolation boundary

```text
Agent container
  • read-only rootfs, dropped capabilities
  • no Bash / web / unrestricted shell tools
  • network: model host via egress proxy only
  • research actions: MCP → ToolGateway only
           │
           ▼
ToolGateway (in zqr-desk)
  • mandate + campaign + family allowances
  • experiments forced to segment=dev
  • plugin code runs in a no-network sandbox
           │
           ▼
SQLite + artifacts under ZQR_HOME
  • one writer discipline per process (WAL)
  • research ledger + session results for audit
```

If isolation checks fail, the research loop refuses to dispatch. Unpriced or unknown model cost stops new spend until a human clears the budget stop.

## 10. Current shape of the product (mental model)

Think of ZQR as three cooperating machines:

1. **Search machine** — campaign loop + agent + gateway + family quotas.  
2. **Judgment machine** — hypotheses, Gates 1–2, holdout closure, research memory.  
3. **Execution machine** — portfolio decision + paper fleet + parity + reports.

The open research question for the operator is not “can the agent run?”—it can—but **whether admitted portfolios improve net wealth under the investment policy**. Architecture exists to make that question answerable without contaminating the evidence.
