# Investment policy

Decided by the human on 2026-10-13, answering section 8 of
`docs/superpowers/specs/2026-10-13-research-library-portfolio-design.md`. These values replace the provisional ones
in that section. Items under "To define" must be settled, in the phase named, before code depends on them.

**Amendment 2026-10-19:** §4 benchmark costs and §6 fee baseline — authoritative profile
`coinbase-lf-allin-v1`; former 0.60%+5 bps retained as `coinbase-retail-taker-v0` stress/contrast.
See §6 and `docs/superpowers/specs/2026-10-19-cost-profiles-mandate.md`.

## 1. Capital and currency

- Quote currency USD. Total paper capital $20,000: BTC-USD portfolio $10,000, ETH-USD portfolio $10,000. The
  combined account is the 50/50 sum (report arithmetic; no shared cash or netted orders).
- No deposits or withdrawals in paper.
- Single-order cap (`max_order_notional`) $10,000, the capital of one product portfolio, so a move from cash to
  fully invested fills in one bar as in the backtest. Raised from $5,000 on 2026-10-15: on 2026-10-14 the cap
  clipped 14 paper orders and moved the clipped strategies' equity 20 to 190 bps away from the backtest.

Decided 2026-10-15 (phase 3, D1): each product portfolio sends one blended order per bar, so the 15% member cap
does not bound order size. Portfolio traders use `max_order_notional` = the portfolio's capital ($10,000): a move
from cash to fully invested is one order and the backtest needs no clip. A lower per-order cap for live trading is
decided with live trading (out of scope); if one is introduced, the backtest must model the same clip.

## 2. Horizon, review and stop rule

- Evaluation horizon: 24 months of paper.
- Review: daily automatic report; monthly review on the first calendar day (mark-to-market, attribution by
  family).
- Soft stop: after 12 months, if the net-of-cost Sharpe ratio of the combined account is significantly below the
  50/50 buy and hold, research dispatch pauses automatically and a full human review starts.

Decided 2026-10-15 (phase 3, D2): "significantly below" is a fixed shortfall, not a significance test (twelve
months of daily returns give a Sharpe standard error of about 1.0, so a test would almost never trigger). On each
monthly review day (the 1st, UTC), once 12 months of paper exist: if the trailing 12-month annualised Sharpe of the
combined account minus that of the 50/50 buy and hold (same capital, same costs, daily marks) is below −0.5,
`desk/soft_stop.json` is written, the research loop refuses to dispatch and the report asks for the human review.
Only `zqr desk soft-stop clear --note` removes it.

## 3. Risk budget

- Maximum drawdown: −30% (hard).
- Ladder: −20% yellow flag in the report and a risk diagnosis; −25% every sleeve halved (half to cash); −30% full
  liquidation to cash and halt until a human intervenes.
- Volatility overlay target σ* = 40% annualised (`voltarget-1m` in the seed catalog).

Decided 2026-10-15 (phase 3, D3):

- The ladder is measured on each product portfolio's own paper equity (independent accounts with independent
  orders); the combined account's drawdown is reported with the yellow flag only.
- Peak: the high-water mark of bar-close equity since the product portfolio started paper.
- A halved portfolio returns to full sleeves when its drawdown recovers to −15% or better.
- After the −30% liquidation only `zqr portfolio resume <name> --note` (human) re-enables trading; resume resets the
  high-water mark to the current equity.
- A change of ladder level always trades, like a full exit, even inside the signal deadband.
- Decided 2026-10-15 (phase 4, P6): `zqr portfolio resume` is run no earlier than 30 days after the liquidation
  and only after a written review in `docs/reviews/`: what caused the drawdown, whether the version's thesis still
  holds, and whether the market regime is one the version's dev covered. Without the review the portfolio stays
  halted or the version is retired. The command enforces both (`--review docs/reviews/<file>.md`).
- The ladder is part of every portfolio version and runs in backtests and paper through one function (appendix A
  section A14).

## 4. Benchmark and cash

- Benchmark: 50/50 buy and hold with the same initial capital and the **same cost profile as the
  mandate under test** (default deployable profile: `coinbase-lf-allin-v1`, see §6).
- Cash earns 0. Any future USDC interest is outside the measured return.

## 5. Research budget

- Annual cap 5% of paper capital: $1,000 a year, $83.33 a month.
- Per campaign: $1 reserved at the start (two to three sessions); stopped if the early sessions show the idea does
  not hold; topped up only after a stage result (for example a passing marginal test on dev), in proportion to the
  family's expected marginal contribution, to at most $5 per campaign.
- Idle with no model calls when the queue is empty or the monthly cap is reached.

Note that $1,000 a year is a 5% annual hurdle on $20,000: the portfolio must beat its benchmark by more than that
before research pays for itself.

Decided 2026-10-15 (phase 4, D7; `docs/superpowers/plans/2026-10-15-phase4-campaigns.md`):

- Reservation: before a session starts, its maximum cost is reserved against the campaign, the month ($83.33) and
  the day; the session is refused if the reservation does not fit. Model calls with unknown pricing stop the loop
  until a price is configured; they are never counted as $0.
- Campaign: $1 reserved at the start. One top-up of $2 when a registered variant passes gate 1 (marginal value on
  dev), a second of $2 when it passes gate 2 (do no harm on validation); never more than $5. No other top-ups.
- Daily cap $5 (was $20), so that one bad day cannot spend two months of budget.

## 6. Fees (cost profiles)

Decided 2026-10-19 (human approval; design note in
`docs/superpowers/plans/2026-10-19-next-features.md` §4 and mandate template
`docs/superpowers/specs/2026-10-19-cost-profiles-mandate.md`).

Research and paper use a **fixed conservative baseline** per mandate. Live venue fees, spreads and
impact are floating; the backtest does **not** model volume-tier schedules or order-book queues.
Costs are environment assumptions with a named profile and a labelled `source`—not strategy
hyperparameters to grid-search.

### 6.1 Authoritative deployable baseline — `coinbase-lf-allin-v1`

| Field | Value | Meaning |
|---|---|---|
| `fee_rate` | **0.0020** (0.20% per side) | All-in commission proxy |
| `slippage_bps` | **2.0** (0.02% per side) | Residual spread / impact buffer |
| Per side | **0.22%** | fee + slippage |
| Round trip | **≈ 0.44%** | 2 × 0.22% |

**Semantic honesty.** The accounting engine still fills at the next bar open with market-style
certainty (`docs/contracts-v2.md` §4). This profile is **not** a maker fill simulator. It is an
**all-in low-frequency rebalancing friction proxy**: a single constant that packs steady-state
maker-biased Coinbase Advanced Trade economics (after normal personal volume crosses the published
tiers), occasional taker cleanup, tight BTC/ETH spreads, and a conservative buffer. The `source`
string on every mandate and experiment must say so explicitly (template in the cost-profiles
mandate spec).

**Why these numbers (calibration sketch, not a live fee API).** For ~$20k paper capital and a few
rebalances per year, 30-day notional typically clears the lowest Coinbase volume tier; maker-biased
execution for hour/day/week cadence is the intended path. A constant near 0.20% fee + 2 bps slip is
a 1.2–1.3× cushion over a plausible steady maker blend, without claiming queue priority or fill
guarantees the engine does not model.

### 6.2 Worst-case retail taker reference — `coinbase-retail-taker-v0`

| Field | Value | Meaning |
|---|---|---|
| `fee_rate` | **0.0060** (0.60% per side) | Coinbase lowest-tier public taker (verified 2026-10-15) |
| `slippage_bps` | **5.0** (0.05% per side) | Prior default slip |
| Per side | **0.65%** | |
| Round trip | **≈ 1.30%** | |

Former system default (policy 2026-10-13 / 2026-10-15). Retained as a **stress / contrast**
profile: “what if every rebalance were worst-case retail market-taking?” Results under this profile
must be labelled; they are not the deployable admission world unless a mandate explicitly chooses
this profile as its base.

### 6.3 Sensitivity and mixing rules

- Mandate acceptance keeps **`require_costs_x2`**: still profitable when the mandate’s **base**
  profile costs are doubled (for `coinbase-lf-allin-v1`, about **0.44% per side / ≈ 0.88% round
  trip**).
- Optional human-run contrast: re-evaluate a frozen candidate under `coinbase-retail-taker-v0`
  without mixing those trials into the deployable profile’s DSR bucket.
- **Do not** mix cost profiles in one split’s research DSR without a new mandate (and normally a
  new split or a recorded mandate change). Profile id and `costs.source` are part of experiment
  identity via `engine_config`.
- Changing the authoritative baseline again requires a dated amendment here, new mandates, and an
  explicit desk/code default change—not a silent host edit.

### 6.4 Historical note

- 2026-10-15: verified Coinbase Advanced Trade lowest tier taker 0.60% / maker 0.40% below $10k
  30-day volume; policy then locked 0.60% + 5 bps as the sole baseline because the account matched
  that tier under a taker assumption.
- 2026-10-19: baseline replaced by `coinbase-lf-allin-v1`; `coinbase-retail-taker-v0` demoted to
  reference/stress. Prior experiments under 0.60% + 5 bps remain valid evidence **for that cost
  world** only.
