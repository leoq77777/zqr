# Investment policy

Decided by the human on 2026-10-13, answering section 8 of
`docs/superpowers/specs/2026-10-13-research-library-portfolio-design.md`. These values replace the provisional ones
in that section. Items under "To define" must be settled, in the phase named, before code depends on them.

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
  halted or the version is retired.
- The ladder is part of every portfolio version and runs in backtests and paper through one function (appendix A
  section A14).

## 4. Benchmark and cash

- Benchmark: 50/50 buy and hold with the same initial capital and the same costs (0.6% taker plus 5 bps).
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

## 6. Fees

- 0.6% taker per side plus 5 bps slippage (0.65% per side, 1.30% per round trip).
- Verified by the human on 2026-10-15 against the Coinbase Advanced Trade fee schedule: the lowest volume tier
  (30-day volume below $10,000) is taker 0.60% / maker 0.40%; the taker rate falls to 0.40% only above $10,000.
  The account's tier matches the assumption, so `fee_rate`, the mandates and every gate threshold stay as they
  are.
