# Long dev, first look 2026-10-15

Rules: plan `docs/superpowers/plans/2026-10-15-phase4-campaigns.md` (E1, F1-F4); appendix A13.4 (split lineage),
A13.5 (isolated bad prints), A13.6 (the ladder in research measures). Release `0.1.0-6e0fb907`.

Everything here is dev data (D6): it may be used in campaign diagnostics and agent briefs. No validation or holdout
number appears; both holdouts stay closed (A13.3, inherited through the lineage).

## What was done

| Step | BTC-USD | ETH-USD |
|---|---|---|
| Pre-2021 candles fetched | 47,745 bars | 40,336 bars |
| Extended dataset | `coinbase-btc-usd-one_hour-bb860569c238` | `coinbase-eth-usd-one_hour-74218a83af9a` |
| Dev from (F1) | 2015-07-21 | 2016-06-01 |
| Coverage, filled bars | 99.94%, 56 | 99.92%, 75 |
| Isolated prints dropped (A13.5) | 2017-04-15 23:00 (open 0.06) | 2017-04-15 23:00 (open 74.98), 2017-06-21 19:00 (low 0.10) |
| Junction jump | 0.0 | 0.03% |
| Split (parent) | `split-f0d7a178e08b` (`split-1016c7c9873a`) | `split-d6a76022a19d` (`split-c47a0be57a24`) |
| Holdout | closed, inherited | closed, inherited |
| Mandate (F3) | `mandate-2af07f137683` | `mandate-32b7824e64dd` |
| Seed trials, T | 12 + 1 portfolio, 3,258 days | 12 + 1 portfolio, 2,942 days |
| K(0.7) own + inherited | 10 + 231 = 241 | 10 + 240 = 250 |
| Seed portfolio, gate 1 reference (F4) | `pv-bef1798ef4d9`, `exp-615efb91a1a4` | `pv-77bad34a29c6`, `exp-5f3de16eb50a` |

The production run reproduced the second rehearsal (same dataset, split, mandate and version ids, same metrics). No
family proposals were created; the parent splits' clusterings were not recomputed.

Gate 1's bar on the long dev (A6, `σ = σ0`): appraisal ratio 0.429 (BTC) and 0.451 (ETH) for one variant, 0.603 /
0.635 for two, 0.715 / 0.752 for three; 0.688 / 0.967 / 1.146 on the 2021 dev.

## Seed portfolios

Daily Sharpe is the A4 statistic; maximum drawdown on bar-close equity; buy and hold has the same capital and costs.
The run without the ladder is the one research measures read (A13.6).

### BTC-USD `pv-bef1798ef4d9`

| Run | Period | Sharpe | Return | Max DD | Exposure | B&H Sharpe | B&H return | B&H max DD |
|---|---|---|---|---|---|---|---|---|
| without ladder | 2015-07-21 to 2020-12-31 | 1.89 | +1,407% | −39% | 33% | 1.51 | +10,221% | −84% |
| without ladder | 2021-01-01 to 2024-06-20 | 0.83 | +88% | −31% | 37% | 0.68 | +124% | −77% |
| without ladder | whole dev | 1.50 | +2,732% | −39% | 35% | 1.22 | +23,014% | −84% |
| with ladder | 2015-07-21 to 2020-12-31 | 1.54 | +556% | −30% | 23% | | | |
| with ladder | 2021-01-01 to 2024-06-20 | 0.00 | 0% | 0% | 0% | | | |
| with ladder | whole dev | 1.20 | +556% | −30% | 14% | | | |

Ladder: halved 2018-01-16, full 2019-05-11, halved 2019-09-26, liquidated 2020-03-12 and halted to the end of dev
(48% of dev in cash).

### ETH-USD `pv-77bad34a29c6`

| Run | Period | Sharpe | Return | Max DD | Exposure | B&H Sharpe | B&H return | B&H max DD |
|---|---|---|---|---|---|---|---|---|
| without ladder | 2016-06-01 to 2020-12-31 | 1.64 | +660% | −33% | 21% | 1.34 | +5,177% | −94% |
| without ladder | 2021-01-01 to 2024-06-20 | 1.11 | +162% | −29% | 33% | 0.96 | +377% | −81% |
| without ladder | whole dev | 1.41 | +1,890% | −33% | 26% | 1.19 | +25,078% | −94% |
| with ladder | 2016-06-01 to 2020-12-31 | 1.56 | +454% | −29% | 17% | | | |
| with ladder | 2021-01-01 to 2024-06-20 | 0.67 | +53% | −30% | 16% | | | |
| with ladder | whole dev | 1.21 | +747% | −30% | 17% | | | |

Ladder: halved and recovered three times in 2017-2020, halved 2022-08-20, liquidated 2023-09-11 and halted to the
end of dev (10% of dev in cash).

## Seed instances

Latest seed run of each instance on the new split; "early" is listing to 2020-12-31, "late" 2021-01-01 to
2024-06-20.

### BTC-USD (B&H Sharpe early 1.51, late 0.68)

| Seed | Sharpe early | Sharpe late | Sharpe whole | Return early | Return late | Max DD early | Max DD late |
|---|---|---|---|---|---|---|---|
| btc-brock-1-200 | 1.57 | 0.70 | 1.27 | +6,224% | +116% | −70% | −67% |
| btc-ewma-8-24 | 1.55 | 0.65 | 1.22 | +1,756% | +79% | −46% | −40% |
| btc-ewma-16-48 | 1.19 | 0.81 | 1.04 | +612% | +125% | −51% | −40% |
| btc-ewma-32-96 | 0.53 | 0.57 | 0.54 | +75% | +59% | −49% | −39% |
| btc-faber-10m | 1.42 | 0.96 | 1.25 | +3,901% | +260% | −74% | −55% |
| btc-tsmom-1w | 0.96 | 0.29 | 0.72 | +667% | +11% | −85% | −65% |
| btc-tsmom-1m | 1.46 | 0.47 | 1.12 | +3,957% | +47% | −69% | −60% |
| btc-tsmom-3m | 1.51 | 0.82 | 1.28 | +6,925% | +165% | −73% | −51% |
| btc-tsmom-12m | 1.24 | 0.89 | 1.12 | +2,555% | +213% | −85% | −55% |
| btc-turtle-s1 | 1.55 | 0.22 | 1.10 | +3,777% | +2% | −63% | −67% |
| btc-turtle-s2 | 1.58 | 0.80 | 1.31 | +4,165% | +133% | −60% | −42% |
| btc-voltarget-1m | 1.53 | 0.53 | 1.16 | +2,310% | +59% | −63% | −68% |

### ETH-USD (B&H Sharpe early 1.34, late 0.96)

| Seed | Sharpe early | Sharpe late | Sharpe whole | Return early | Return late | Max DD early | Max DD late |
|---|---|---|---|---|---|---|---|
| eth-brock-1-200 | 1.53 | 1.20 | 1.40 | +7,465% | +649% | −84% | −61% |
| eth-ewma-8-24 | 0.96 | 0.66 | 0.83 | +359% | +102% | −55% | −52% |
| eth-ewma-16-48 | 0.88 | 0.87 | 0.88 | +317% | +176% | −61% | −35% |
| eth-ewma-32-96 | 0.65 | 0.98 | 0.79 | +148% | +235% | −78% | −35% |
| eth-faber-10m | 0.93 | 1.08 | 0.99 | +602% | +479% | −95% | −61% |
| eth-tsmom-1w | 1.05 | 1.16 | 1.07 | +1,024% | +449% | −81% | −46% |
| eth-tsmom-1m | 1.41 | 0.77 | 1.17 | +4,920% | +176% | −74% | −65% |
| eth-tsmom-3m | 1.46 | 0.95 | 1.26 | +6,078% | +327% | −85% | −72% |
| eth-tsmom-12m | 0.47 | 1.14 | 0.75 | +42% | +564% | −91% | −60% |
| eth-turtle-s1 | 2.01 | 1.11 | 1.67 | +30,918% | +394% | −41% | −46% |
| eth-turtle-s2 | 1.70 | 0.70 | 1.32 | +8,296% | +121% | −68% | −66% |
| eth-voltarget-1m | 1.02 | 0.88 | 0.96 | +422% | +170% | −74% | −61% |

## Observations

1. **The early years were better for trend, and for the market.** On BTC 11 of 12 seeds have a lower Sharpe after
   2021 (only `ewma-32-96` holds, at a low level); buy and hold falls from 1.51 to 0.68 over the same cut. On ETH 7
   of 12 fall and `ewma-16-48` is flat; the slow rules (`ewma-32-96`, `faber-10m`, `tsmom-12m`) and `tsmom-1w` rise. The fast and breakout
   rules lose the most on both products (`turtle-s1` BTC 1.55 to 0.22, `tsmom-1m` 1.46 to 0.47 and 1.41 to 0.77).
2. **Against buy and hold the seeds' Sharpe is a coin flip; their drawdown is not.** In each period about half the
   seeds beat buy and hold's Sharpe (BTC 5 of 12 early and 6 late; ETH 5 early, 6 late). Their maximum drawdowns
   (−35% to −95%) are smaller than buy and hold's (−77% to −94%) in all but three of 48 cases (BTC `tsmom-1w` and
   `tsmom-12m` early, ETH `faber-10m` early, each within a point).
3. **The portfolio is where the seeds add up.** Without the ladder the seed portfolio beats buy and hold's Sharpe in
   both periods on both products (BTC 1.89 / 0.83 against 1.51 / 0.68; ETH 1.64 / 1.11 against 1.34 / 0.96), with a
   maximum drawdown of −39% (BTC) and −33% (ETH) over nine and eight years. The price is return: exposure is 21-37%,
   and buy and hold earns 8 (BTC) and 13 (ETH) times more over the whole dev. This is gate 1's `R_p`: a variant must add hedged
   Sharpe on top of a reference with a dev Sharpe of 1.50 (BTC) and 1.41 (ETH).
4. **A −30% liquidation is a real event on this horizon.** The ladder liquidated BTC on 2020-03-12 and ETH on
   2023-09-11. With the ladder, BTC sat in cash through 2021-2024. In paper and live a liquidation waits for a
   human resume; the investment policy does not say when a human resumes. Not needed now (no portfolio is in paper),
   but needed before one is (open point below).
5. **Data quality before 2018 is lower.** Three isolated prints were dropped (A13.5); ETH has many one-bar gaps in
   June-July 2016; the cost model is today's, which probably understates costs in the thin 2015-2017 books.

## Caveats

- **Out of sample for the parameters, not for the idea.** 11 of the 12 seed parameter sets come from studies of
  traditional assets published 1992-2018 (Brock et al. 1992, Faber 2007, Faith 2007, Moskowitz et al. 2012, Baz et
  al. 2015, Hurst et al. 2017, Moreira and Muir 2017, Harvey et al. 2018); `tsmom-1w` comes from Liu and Tsyvinski
  (2021), whose crypto sample covers these early years, so its early numbers are in sample. The choice to research
  trend on crypto at all was made knowing how 2017 and 2020-2021 went.
- **Two periods, one cut.** 2021-01-01 is where the old dev began, not a regime boundary chosen on the data; with
  two periods per product, "decay" and "a weaker market" cannot be told apart.
- **Gate 1 uses the whole dev.** The early years carry about 60% of each product's dev days, and they are the
  stronger years for the reference: a variant is judged mostly on how it hedges against trend in 2015-2020.

## Open points for the human

- A resume procedure after a liquidation, for the investment policy (before any portfolio version reaches paper).
- Whether gate 1 should also report the hedged Sharpe per period (early and late) next to the whole-dev statistic,
  as a diagnostic only; the gate stays on the whole dev (A6).

## Next

Research stays disabled. Phase 4 Tasks 3-16 (registration, gates 1-2, campaigns) are detailed on the long dev.
