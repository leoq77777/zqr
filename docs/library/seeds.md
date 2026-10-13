# Seed catalog

Status: draft for human review (phase 1, design section 3). Nothing here is imported until approved. The machine
copy is `library/seeds.yaml`, generated from this catalog after review; `zqr library import` reads it.

Written from the sources only. No ZQR data (dev, validation or holdout) was looked at while drafting, and no
parameter was chosen by testing. Turnover, cost and correlation figures appear only after import, in the dev
characterisation run (design 3.3), and are not used to change this list.

## 1. Conventions

### 1.1 Time translation

Bars are hourly, UTC, and crypto trades every calendar day.

| Source unit | Bars | Rule |
|---|---|---|
| 1 day | 24 | calendar day |
| 1 week | 168 | 7 days |
| 1 month | 720 | 30 days; N months = N × 720 (10 months = 7,200) |
| 12 months | 8,760 | one year = 365 days, not 12 × 720 = 8,640 (review point R1) |
| N daily bars (Donchian, Turtle, Brock, Baz spans) | 24 × N | daily bars are calendar days in crypto, so the count is kept: 20 days = 480 |
| Windows that stand for a calendar period (Baz: 63 days = a quarter, 252 days = a year) | by calendar | 63 days → 3 months = 2,160; 252 days → 1 year = 8,760 (review point R2) |

### 1.2 Decision cadence

Each source decides at its own data frequency. A template with a cadence computes its signal on every bar, keeps
only the value at the cadence boundaries and holds it until the next boundary. The boundary bar is the one closing
at 00:00 UTC; its decision fills at the next bar's open, as for every strategy (exposure contract v0.2). Before the
first boundary with a complete lookback, the exposure is 0. Holding a past value never uses later data.

| Cadence | Boundary (bar closing at 00:00 UTC on) |
|---|---|
| hourly | every bar |
| daily | every day |
| weekly | Monday |
| monthly | the 1st of the month |

Cadence is part of the canonical rule, not a tuning choice, and it is the main protection against turnover at
0.6% per side (review point R3).

### 1.3 Positions and sizing

Members output 0 or 1 (binary) unless noted. Several sources size each position by inverse volatility (Moskowitz,
Ooi & Pedersen scale to 40% annual volatility; the Turtles size by N). For long/cash with a cap of 1, that sizing
is not done per member; it is the portfolio volatility overlay (section 6), applied once to the blend.

## 2. Structure

Four families take a sleeve; the volatility overlay is not a sleeve.

| Family | Variants | Sleeve | Share per member |
|---|---|---|---|
| Time-series momentum (`tsmom`) | 4 | 25% | 6.25% |
| EWMA trend (`ewma_trend`) | 3 | 25% | 8.33% |
| Channel breakout (`channel_breakout`) | 2 | 25% | 12.5% |
| Trend filter (`trend_filter`) | 2 | 25% | 12.5% |
| Volatility overlay (`vol_overlay`) | 1 | — | scales the blend |

Every sleeve family has at least two variants, so no member exceeds the 15% single-member cap (design 5.2). The
same 11 entries are instantiated for BTC-USD and ETH-USD with identical parameters (22 instances).

Seeds are imported with status `candidate`. They become `member` when the seed portfolio version enters paper
(design 5.8); seeds have no probation.

## 3. Time-series momentum (`tsmom`)

Thesis: an asset's own trailing return predicts its next return; the effect holds across asset classes and a
century of data, and in crypto at horizons of one to four weeks.

Sources:

- Moskowitz, Ooi & Pedersen (2012), "Time series momentum", *Journal of Financial Economics* 104(2). Canonical
  rule: sign of the trailing 12-month return, rebalanced monthly.
- Hurst, Ooi & Pedersen (2017), "A century of evidence on trend-following investing", *Journal of Portfolio
  Management* 44(1). Equal-weighted 1-, 3- and 12-month signals, rebalanced monthly.
- Liu & Tsyvinski (2021), "Risks and returns of cryptocurrency", *Review of Financial Studies* 34(6). The current
  week's return predicts returns one to four weeks ahead. Their sample (2011–2018) ends before our data starts
  (2021), so it is an out-of-sample prior.

Template `tsmom` (new): exposure 1 when `close[t] / close[t − lookback] − 1 > 0`, else 0, sampled at the cadence.

| Entry | Lookback | Bars | Cadence | Source |
|---|---|---|---|---|
| `tsmom-1w` | 1 week | 168 | weekly | Liu & Tsyvinski |
| `tsmom-1m` | 1 month | 720 | monthly | Hurst, Ooi & Pedersen |
| `tsmom-3m` | 3 months | 2,160 | monthly | Hurst, Ooi & Pedersen |
| `tsmom-12m` | 12 months | 8,760 | monthly | Moskowitz, Ooi & Pedersen; Hurst, Ooi & Pedersen |

Adaptation notes:

- Long/cash: a negative signal means cash, not a short.
- Liu & Tsyvinski rebalance weekly, so `tsmom-1w` decides on Mondays only. It is the fastest seed and the most
  exposed to costs (review point R4).
- The design mentioned a continuous variant that scales the position with signal strength. No source gives a
  long/cash mapping without a free parameter (where the position reaches 1), so it is not seeded. The template
  keeps the binary rule only (review point R5).

## 4. EWMA trend (`ewma_trend`)

Thesis: the same trend premium measured by smoothed price crossovers with a continuous response, which trades
less on weak signals than a binary crossover.

Source: Baz, Granger, Harvey, Le Roux & Rattray (2015), "Dissecting investment strategies in the cross section
and time series", SSRN 2695101. The trend signal for a pair of time scales (S, L) in days:

1. `x = EWMA_S(price) − EWMA_L(price)`, EWMA with time scale n (decay 1 − 1/n);
2. `y = x / std_63(price)` (63-day standard deviation of price);
3. `z = y / std_252(y)` (252-day standard deviation of y);
4. response `u = z · exp(−z² / 4) / 0.89`, which lies in [−1, 1] and peaks at 1 at `z = √2`.

The exact EWMA and window definitions are checked against the paper text before import.

Template `ewma_trend` (new): exposure `max(0, u)`, computed on daily closes (the close of the bar ending 00:00
UTC), cadence daily.

| Entry | S, L (days) | Bars | Normalisation windows |
|---|---|---|---|
| `ewma-8-24` | 8, 24 | 192, 576 | 3 months (2,160), 1 year (8,760) |
| `ewma-16-48` | 16, 48 | 384, 1,152 | same |
| `ewma-32-96` | 32, 96 | 768, 2,304 | same |

Adaptation notes:

- Long/cash: negative u means cash. The response function shrinks exposure for very strong signals (z beyond √2);
  this is the source's design, kept as is.
- The paper uses trading days (252 a year). Spans keep their day counts; the two normalisation windows stand for a
  quarter and a year and translate by calendar (review point R2).
- The 0.89 constant normalises the long/short response; for long/cash it only sets where the exposure reaches 1.
  It is kept, not re-fitted.

## 5. Channel breakout (`channel_breakout`)

Thesis: a new N-day high marks the start of a trend; exit on a shorter opposite channel.

Sources: Faith (2007), *Way of the Turtle* (the Turtle rules); Donchian channels. System 1 enters on a 20-day
breakout and exits on a 10-day opposite breakout; System 2 uses 55 and 20 days.

Template `breakout` (existing, version 1): enter when the close exceeds the highest high of the prior `entry`
bars, exit when the close falls below the lowest low of the prior `exit` bars. Cadence hourly: the Turtles traded
breakouts intraday.

| Entry | Entry, exit (days) | Bars |
|---|---|---|
| `turtle-s1` | 20, 10 | 480, 240 |
| `turtle-s2` | 55, 20 | 1,320, 480 |

Adaptation notes:

- Long only; short breakouts mean cash.
- Not adopted: System 1's rule to skip a breakout after a winning one (it needs a simulated shadow trade), the 2N
  stop, pyramiding up to four units, and N-based sizing. Sizing is the portfolio overlay's job (1.3).
- The channel uses hourly highs and lows over 24 × N bars, a rolling N-day window rather than N completed
  calendar days.
- Stateful: the position depends on the path since entry. Phase 0's full-history rebuild covers this (history
  requirement: the longest channel plus the time since the last entry, i.e. the full store).

## 6. Trend filter (`trend_filter`)

Thesis: hold the asset only while it trades above its long moving average; this avoids most of the deepest
drawdowns at the cost of lagging re-entries.

Sources:

- Faber (2007), "A quantitative approach to tactical asset allocation", *Journal of Wealth Management* 9(4).
  Canonical rule: long while the month-end price is above the 10-month simple moving average, checked monthly.
- Brock, Lakonishok & LeBaron (1992), "Simple technical trading rules and the stochastic properties of stock
  returns", *Journal of Finance* 47(5). Variable moving-average rules on daily data; the "1-200" rule is long while
  the price is above its 200-day average, with no band.

Template `ma_filter` (new): exposure 1 when `close > SMA(window)`, sampled at the cadence.

| Entry | Window | Bars | Cadence | Source |
|---|---|---|---|---|
| `faber-10m` | 10 months | 7,200 | monthly | Faber |
| `brock-1-200` | 200 days | 4,800 | daily | Brock, Lakonishok & LeBaron |

Adaptation notes:

- Faber averages ten month-end prices; here the average runs over all 7,200 hourly closes. It is smoother and
  reacts slightly differently, but it keeps the agreed bar translation.
- The design listed Brock et al. under moving-average trend. The 1-200 rule is a price filter, not a crossover of
  two averages, so it sits here, which also gives this family its second variant.

## 7. Volatility overlay (`vol_overlay`)

Thesis: volatility clusters while expected returns do not rise proportionally, so cutting exposure when realised
volatility is high improves risk-adjusted returns, especially for risk assets.

Sources:

- Moreira & Muir (2017), "Volatility-managed portfolios", *Journal of Finance* 72(4). Exposure scaled by the
  inverse of the previous month's realised variance from daily returns.
- Harvey, Hoyle, Korgaonkar, Rattray, Sargaison & Van Hemert (2018), "The impact of volatility targeting",
  *Journal of Portfolio Management* 45(1). Volatility targeting (inverse volatility) raises Sharpe ratios for risk
  assets and lowers tail risk.

Template `vol_target` (new): `scale = min(1, σ* / σ̂)`, with σ̂ the standard deviation of the last 30 daily
returns (720 bars, daily closes), annualised by √365, recomputed daily. Standalone it is a volatility-targeted
buy and hold, so it can be tested like any template; in the portfolio it multiplies the blend (design 5.2).

| Entry | Window | Bars | Cadence | Target σ* |
|---|---|---|---|---|
| `voltarget-1m` | 1 month | 720 | daily | from the risk budget (open decision, section 8 of the design) |

Adaptation notes:

- Inverse volatility (Harvey et al.) rather than inverse variance (Moreira & Muir): with a cap of 1, inverse
  variance mostly hits the cap or cuts very hard, and the design's overlay formula is in volatility.
- The cap at 1 means the overlay only reduces exposure, never levers.
- Not a sleeve and not a blend member; at most one overlay is active per portfolio. σ* must be set before the
  seed portfolio is validated.

## 8. Considered and not seeded

| Idea | Reason |
|---|---|
| Short-term reversal (daily to weekly) | Documented, but the edge per trade is far below the 1.3% round-trip cost (0.6% per side plus 5 bps slippage). Recorded in the archive as infeasible under costs |
| Continuous TSMOM | No source mapping to long/cash without a free parameter (R5) |
| Cross-sectional momentum | Needs a cross-section; the universe has two assets |
| Carry, basis, funding | Needs derivatives or shorting; out of scope (spot, long/cash) |
| Brock 1-50, 1-150, 5-150 and banded rules | Same family as `brock-1-200`; more variants of one source add correlation, not evidence. Kept as candidates for later registered hypotheses |

## 9. Known concentration

Three of the four sleeves are trend following at different speeds (`tsmom-12m`, `faber-10m` and `brock-1-200`
are all long-horizon trend). This is expected: the published low-turnover evidence for long/cash crypto is almost
all trend. Diversification here is by speed and signal shape, not by premium. Phase 2 clustering will show how
many independent families these really are; family definitions are confirmed then, before the seed portfolio is
fixed (review point R6).

## 10. Review points

- **R1.** 12 months = 8,760 bars (one year), while N months below 12 use 30-day months (10 months = 7,200).
  Alternative: 12 × 720 = 8,640.
- **R2.** Baz et al.'s 63- and 252-day normalisation windows translate as a quarter and a year (2,160 and 8,760
  bars). Alternative: keep the day counts (1,512 and 6,048).
- **R3.** Decision cadence follows each source (weekly, monthly, daily, hourly for breakouts). Alternative: all
  daily.
- **R4.** Keep `tsmom-1w` (crypto-specific, out-of-sample, but the most cost-exposed), or drop it and keep
  Hurst's 1/3/12 months only (3 variants, 8.33% each).
- **R5.** No continuous TSMOM seed.
- **R6.** Accept the trend concentration for now and revisit after the phase 2 clustering.
