# Investment policy

Decided by the human on 2026-10-13, answering section 8 of
`docs/superpowers/specs/2026-10-13-research-library-portfolio-design.md`. These values replace the provisional ones
in that section. Items under "To define" must be settled, in the phase named, before code depends on them.

## 1. Capital and currency

- Quote currency USD. Total paper capital $20,000: BTC-USD portfolio $10,000, ETH-USD portfolio $10,000. The
  combined account is the 50/50 sum (report arithmetic; no shared cash or netted orders).
- No deposits or withdrawals in paper.
- Single-order cap (`max_order_notional`) stays $5,000.

To define (phase 3): each product portfolio sends one blended order per bar, so the 15% member cap does not bound
order size. A $10,000 portfolio moving from cash to fully invested (all trend signals turning together, or
re-entering after a flatten) needs an order of about $10,000; paper clips it to $5,000 and completes it a bar later,
which the backtest does not do. Either the portfolio traders get a cap of at least their capital, or the backtest
models the same clip.

## 2. Horizon, review and stop rule

- Evaluation horizon: 24 months of paper.
- Review: daily automatic report; monthly review on the first calendar day (mark-to-market, attribution by
  family).
- Soft stop: after 12 months, if the net-of-cost Sharpe ratio of the combined account is significantly below the
  50/50 buy and hold, research dispatch pauses automatically and a full human review starts.

To define (phase 3): "significantly below" as a number. Twelve months of daily returns give a Sharpe standard error
of about 1.0, so a test at the usual significance would almost never trigger; a practical rule is a fixed shortfall
(for example, the Sharpe difference below −0.5) rather than a significance test.

## 3. Risk budget

- Maximum drawdown: −30% (hard).
- Ladder: −20% yellow flag in the report and a risk diagnosis; −25% every sleeve halved (half to cash); −30% full
  liquidation to cash and halt until a human intervenes.
- Volatility overlay target σ* = 40% annualised (`voltarget-1m` in the seed catalog).

To define (phase 3): the equity the drawdown is measured on (combined account or each product portfolio) and its
peak (high-water mark since the portfolio version started); when a halved portfolio returns to full sleeves.

## 4. Benchmark and cash

- Benchmark: 50/50 buy and hold with the same initial capital and the same costs (0.6% taker plus 5 bps).
- Cash earns 0. Any future USDC interest is outside the measured return.

## 5. Research budget

- Annual cap 5% of paper capital: $1,000 a year, $83.33 a month.
- Per campaign: $1 reserved at the start (two to three sessions); stopped if the early sessions show the idea does
  not hold; topped up only after a stage result (for example a passing marginal test on dev), in proportion to the
  family's expected marginal contribution, to at most $5 per campaign.
- Idle with no model calls when the queue is empty or the monthly cap is reached.

To define (phase 4): the stage results that allow a top-up and the top-up amounts, as deterministic controller
rules. Note that $1,000 a year is a 5% annual hurdle on $20,000: the portfolio must beat its benchmark by more than
that before research pays for itself.

## 6. Fees

- Working assumption: 0.6% taker per side plus 5 bps slippage, unchanged for phases 1 and 2.
- To verify (before phase 3): the account's actual tier on Coinbase's Trade Fees page. If the taker fee is above
  0.6%, `fee_rate` rises before the phase 3 release and every gate threshold is recomputed.
