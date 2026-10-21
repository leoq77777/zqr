# Next features — research process, models, harness, costs

**Status:** draft for human review (2026-10-19). **D4 cost profiles approved and wired** the same
day (see §4 Decisions): policy, mandate field, `backtest.costs`, CLI/desk/roster defaults, tests.
Other directions (D1–D3) still need explicit decisions. Policy: `docs/investment-policy.md` §6;
spec: `docs/superpowers/specs/2026-10-19-cost-profiles-mandate.md`.

**Goal:** Record known bottlenecks in strategy research throughput and quality, and the candidate
directions to address them—without weakening the invariants that make ZQR’s results defensible.

**Context:** Desk is a personal systematic research + paper system (Coinbase BTC/ETH spot, long/cash,
hourly bars). Deterministic code owns validation, DSR, holdout, and promotion. The agent proposes;
it does not set candidate state. Live trading remains out of scope. Related reading:
`docs/development.md` §12, `docs/investment-policy.md` §6, `docs/zqr_project_architecture.md`,
phase-4 plan `docs/superpowers/plans/2026-10-15-phase4-campaigns.md`.

**Invariant (do not trade away casually):**

1. Model opinion never changes candidate / portfolio state.
2. Holdout and validation numbers stay out of agent-facing loops (no snooping).
3. Costs and acceptance rules are mandate assumptions with labelled sources—not silent knobs.
4. No trading credentials; paper stays local simulation unless a later phase says otherwise.

---

## 1. Multi-agent expert review (research steering)

### Problem

Today’s loop is effectively **single-agent exploration** under **hard, predeclared thresholds**
(mandate acceptance, gates 1–2, DSR, holdout caps). That is good for auditability, but weak for
*process* flexibility:

- One agent carries topic choice, hypothesis framing, code, and self-critique.
- Hard fails (e.g. cost-dominated, mostly-beta, DSR) become dead ends rather than structured
  redirection of the research portfolio’s **direction and weight** (which families, cadences, and
  campaign budgets deserve more sessions).
- Campaign generation already has some steering; it is not yet an explicit multi-role review.

### Direction

Introduce a **multi-agent (or multi-role) expert review** layer that advises **research allocation**,
not statistical truth:

| Role (illustrative) | Input | Output (advisory) |
|---|---|---|
| Explorer / implementer | campaign brief, gateway tools | experiments, plugins, `research.json` |
| Critic / risk | diagnostics, labels, family archive | “stop / pivot / concentrate” notes |
| Allocator | queue, spend, clustering K, soft-stop | suggested weights across families / topics |
| Human | reviews + standing policy | approve queue, budgets, mandate changes |

### Constraints

- Review agents may change **campaign queues, topic weights, session budgets, and human-facing
  recommendations** only through recorded, reviewable artifacts.
- They must **not** lower gate thresholds, reopen holdouts, or mark candidates `validated`.
- Blind Gate 2 and holdout isolation stay as today.
- Prefer a small, fixed role graph over an open “committee of N” that burns the research budget.

### Open decisions

- D1.1: Advisory-only vs ability to auto-reorder the campaign queue within human-approved caps.
- D1.2: Same model for all roles vs stronger model for critic/allocator only.
- D1.3: Where artifacts live (`desk/` reviews vs `docs/reviews/`) and what the daily report shows.

### Success signal

Fewer wasted sessions on families already known to die under friction; higher fraction of sessions
that either admit a hypothesis or write a reusable negative conclusion—without DSR inflation from
uncontrolled retry loops.

---

## 2. Stronger models for think-intensive research

### Problem

The desk currently runs through an Anthropic-compatible API with **DeepSeek flash** as the default
(`docs/development.md`: harness Claude Code; provider DeepSeek). Flash-class models are cheap and
fast enough for high session volume, but strategy research is **think-intensive**:

- reading diagnostics and attribution,
- proposing economically plausible variants (not fee-arbitrage noise),
- writing sandbox-safe plugins,
- knowing when to stop.

Capacity limits show up as shallow pivots, high-frequency attempts that clustering already rejected
as deterministic losses under friction, and weak use of prior family conclusions.

### Direction

- Treat **model tier as a campaign / role preset**, not a global silent default:
  - flash (or equivalent) for mechanical explore / boilerplate,
  - pro / reasoning-capable models for critic, registration write-ups, and hard campaigns.
- Use existing attribution (`zqr_version`, `harness`, `model` on session ledger) and extend the
  daily report to break outcomes by model (already noted as a gap in `development.md` §12).
- Rank models on **benchmark + desk proxies** (qualified registrations per dollar, gate-1 pass rate,
  audit cleanliness)—not on a single lucky Sharpe.

### Open decisions

- D2.1: Which models are allowed in `campaign_models` / price presets; monthly spend envelope if
  pro share rises.
- D2.2: Mandatory “escalation”: e.g. after N flash failures in a family, one pro session before
  concluding the campaign.
- D2.3: Whether A/B of flash vs pro runs on the same frozen campaign brief (costly but clean).

### Success signal

Higher rate of economically coherent hypotheses per dollar; fewer sessions labelled as friction
suicide; report can attribute quality to model tier.

---

## 3. Research-specific agent harness (beyond Claude Code / Codex)

### Problem

Current and contemplated harnesses (**Claude Code**, **Codex**) are **general-purpose coding
agents**. Strategy research needs a narrower loop:

- fixed tools (MCP gateway only; no Bash / web),
- experiment submit → wait → diagnose → next trial,
- hard stop on budget / stop file / isolation failure,
- prompts and skills that encode ZQR contracts (segments, costs, no validation peeking).

A general harness optimises for “finish a software task,” not “maximise defensible evidence under
a mandate.” A second adapter (Codex) is already listed as not built (`development.md` §12); adding
another generic harness without a research-shaped workflow may not help.

### Direction

- Short term: **prompt / skill / ROLE.md fine-tuning** on the existing Claude Code path—research
  playbooks, diagnosis→action maps, “do not propose hourly churn under 1.3% RT” guardrails,
  citation of family archives.
- Medium term: evaluate a **custom thin harness** (or a thin supervisor over Claude Code/Codex)
  whose only job is the research state machine: brief → tool calls → freeze → audit → classify.
- Keep isolation, egress allowlist, and sandbox as they are; the harness change is orchestration
  and prompting, not a relaxation of security.

### Open decisions

- D3.1: Invest in skills on Claude Code first vs spike a custom runner against the same gateway.
- D3.2: Whether Codex is worth a second adapter for diversity, or deferred until the research loop
  shape is fixed.
- D3.3: Benchmark tasks for harness comparison (extend v0.2 or a desk-shaped micro-bench).

### Success signal

Same model + same mandate produces better session taxonomy (fewer environment/harness errors, more
conclusions, cleaner audits) under the custom or skill-tuned path than under stock Claude Code.

---

## 4. Cost model: named profiles (D4)

### Problem

The sole baseline was **0.6% taker + 5 bps ≈ 1.30% round-trip** (lowest-tier Coinbase taker,
verified 2026-10-15). That world is valid as a stress case but, used alone, starves low-frequency
research and invites the false conclusion “no edge” when part of the failure is an overly harsh
fixed friction assumption. Fees remain an **environment / mandate assumption**, not a
grid-searched hyperparameter.

### Direction (approved shape)

Named profiles, fixed constants, labelled `source`, `costs_x2` on the mandate base, no mixing of
profiles inside one DSR bucket without a new mandate. Engine fill semantics stay next-open
market-style (`contracts-v2` §4)—no maker queue simulator in this change.

### Decisions (approved by the human 2026-10-19)

| Id | Decision |
|---|---|
| **D4.1** | Deployable account remains **Coinbase Advanced Trade** (BTC/ETH spot). Authoritative research/paper baseline is **`coinbase-lf-allin-v1`**: `fee_rate=0.0020`, `slippage_bps=2.0` (≈ **0.22% per side**, ≈ **0.44% round trip**). This is an **all-in low-frequency rebalancing friction proxy**, not a claim of maker fills. |
| **D4.2** | **`coinbase-retail-taker-v0`** (`fee_rate=0.0060`, `slippage_bps=5.0`, ≈ **1.30% RT**) is demoted to **worst-case retail taker contrast/stress**. Explore/admit: admission and desk defaults use lf-allin-v1; taker-v0 may be run as a labelled contrast (including on frozen candidates) and must **not** share the deployable mandate’s DSR bucket. A campaign may use taker-v0 as its **base** only when the written question is explicitly that stress world. |
| **D4.3** | Policy amended in `docs/investment-policy.md` §6 (and §4 benchmark “same costs as mandate”). Mandate template + canonical `source` strings in `docs/superpowers/specs/2026-10-19-cost-profiles-mandate.md`. **Code defaults, `Mandate.cost_profile` validator, desk setup, and roster defaults change only via a follow-up implementation plan**—no silent host edit. Old mandates and experiments stay evidence for their recorded cost world. **`require_costs_x2` stays** (≈ 0.88% RT under lf-allin-v1). |

### Semantic honesty (recorded with the approval)

- Reject naming the new baseline “maker fees” while the engine still grants certain next-open fills.
- All-in constant + explicit `source` is the interim institutional pattern until a real limit-order
  simulator exists.
- Dual track: lf-allin-v1 for deployable screening; `costs_x2` for robustness; retail-taker-v0 for
  extreme market-taking contrast.

### Success signal

Clear separation between “no edge under the deployable all-in baseline” and “fails only under
worst-case retail taking.” New mandates and reports state `cost_profile`.

---

## Suggested sequencing (for discussion)

| Priority | Direction | Why this order |
|---|---|---|
| P0 | **D4 cost profiles** — **done** (policy + code) | New desk mandates / CLI default to lf-allin-v1; old mandates unchanged |
| P1 | **D2 model tiering** + report by model | Immediate quality/$; uses existing ledger fields |
| P2 | **D3 skills / ROLE playbooks** | Cheap harness gain before building a custom runner |
| P3 | **D1 multi-agent review** | Highest design risk; depends on clearer allocation metrics |
| Later | Custom harness spike; Codex adapter; non-crypto venues | Only after P0–P2 show the bottleneck is orchestration |

---

## Explicitly out of scope here

- Live exchange credentials or automated live trading.
- Relaxing holdout caps, DSR accounting, or blind Gate 2 to “get a pass.”
- Silently lowering `fee_rate` without a dated policy amendment and new mandates.
- Labelling next-open fills as “maker execution.”
- Replacing deterministic acceptance with an LLM vote.

---

## Next step

- **D4 (done):** policy §6, spec, and code wiring. **Ops:** create new desk mandates (or
  `zqr desk setup` on a fresh home) so production uses lf-allin-v1; existing immutable mandates
  stay on their recorded costs until replaced.
- **D1–D3:** still open for human review; no code until decided.

Approved decisions become dated implementation plans under `docs/superpowers/plans/` with tasks
and release notes before they change running desk behaviour.
