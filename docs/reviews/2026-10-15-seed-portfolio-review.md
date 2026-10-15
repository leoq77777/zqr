# Seed portfolio review 2026-10-15

Rules: design `docs/superpowers/specs/2026-10-13-research-library-portfolio-design.md` 5.8; appendix A6 (seed
validation standard, gate 3), A9 (holdout budget), A14; investment policy sections 1-4. Release `0.1.0-ed285a5f`.

This document holds validation and holdout metrics. It is for the human only: it must not be copied into session
briefs, family archives or any agent-facing view (`docs/` is not part of the runtime or agent image).

## Outcome

| Version | Product | Split | Final state | Decided by |
|---|---|---|---|---|
| `pv-4864fc813a30` | BTC-USD | `split-1016c7c9873a` | `failed_final` (gate 3) | evaluator, after the human froze it |
| `pv-6b52caaf11b5` | ETH-USD | `split-c47a0be57a24` | `failed_validation` (Sharpe 0.35 < 0.5) | controller |

Neither seed portfolio reaches paper. Both states are terminal; under A6 neither version is adjusted and retried.
The 16 single-strategy controls keep running in paper.

Timeline (UTC): seed and validate 2026-10-15 03:41; BTC frozen 03:48 with the note "Seed portfolio passed
validation with Sharpe 1.094 and maxDD -19.2% across 4 canonical trend/momentum families without data snooping.";
holdout 03:48, first viewing of the split (`exp-be2be41752b2`, no override).

The production run reproduced the local rehearsal on a copy of production data (same version ids; metrics equal to
about 15 significant digits, the residue being floating-point order on amd64 against arm64).

## Results

Daily Sharpe is the A4 statistic; maximum drawdown on bar-close equity; buy and hold has the same capital and costs.
Segments: dev 2021-01-01 to 2024-06-20, validation 2024-06-20 to 2025-08-17, holdout 2025-08-17 to 2026-10-13.

### BTC-USD `pv-4864fc813a30`

| | dev | validation | holdout |
|---|---|---|---|
| Daily Sharpe | 0.75 | 1.09 | **−0.90** |
| Total return | +57.7% | +34.6% | **−16.1%** |
| Max drawdown (without ladder) | −24.8% (−24.8%) | −19.2% (−19.2%) | −24.0% (−24.0%) |
| Turnover | 10.4 | 6.0 | 7.1 |
| Fees / fills | $641 / 84 | $414 / 43 | $353 / 41 |
| Exposure (mean target) | 30% | 56% | 30% |
| Ladder: time at full / yellow flag | 100% / 12% | 100% / 0% | 100% / 45% |
| Buy and hold: Sharpe / return / max drawdown | 0.68 / +122% / −77% | 1.30 / +80% / −31% | −0.47 / −29.3% / −54% |
| Standard | | Sharpe ≥ 0.5, max DD ≥ −25%: passed | Sharpe ≥ 0.5, return > 0: **failed** |

Family contribution (change in equity as a share of initial capital):

| Family | dev | validation | holdout |
|---|---|---|---|
| trend_filter | +20.3% | +10.5% | −2.9% |
| tsmom | +15.9% | +9.2% | −5.8% |
| channel_breakout | +12.6% | +5.6% | −4.1% |
| ewma_trend | +9.3% | +9.3% | −3.1% |
| none (fees between targets) | −0.3% | 0.0% | −0.3% |

### ETH-USD `pv-6b52caaf11b5`

| | dev | validation |
|---|---|---|
| Daily Sharpe | 0.52 | **0.35** |
| Total return | +33.9% | +6.0% |
| Max drawdown (without ladder) | −25.2% (−25.3%) | −19.3% (−19.3%) |
| Turnover | 9.7 | 3.8 |
| Fees / fills | $602 / 76 | $201 / 29 |
| Exposure | 25% | 24% |
| Ladder: time at full / halved / yellow flag | 93% / 7% / 22% | 100% / 0% / 0% |
| Buy and hold: Sharpe / return / max drawdown | 0.96 / +375% / −81% | 0.62 / +25% / −65% |
| Standard | | Sharpe ≥ 0.5, max DD ≥ −25%: **failed** |

Validation family contribution: channel_breakout +5.4%, ewma_trend +2.2%, tsmom +0.5%, trend_filter −2.1%.

The ETH holdout was not viewed (0 of 10).

## BTC holdout path

BTC rose to about 125,000 in early October 2025, fell to 58,190 on 2026-06-25 (−54% from the peak) and ended at
83,576.

| Quarter | Portfolio | BTC | Mean exposure |
|---|---|---|---|
| 2025 Q3 (from 08-17) | −4.8% | −2.8% | 63% |
| 2025 Q4 | −13.3% | −23.3% | 31% |
| 2026 Q1 | −3.3% | −22.0% | 9% |
| 2026 Q2 | −2.8% | −14.2% | 23% |
| 2026 Q3 | +8.2% | +42.8% | 34% |
| 2026 Q4 (to 10-13) | −0.1% | 0.0% | 82% |

- The portfolio entered the holdout 94% invested: the members' signals were warm and long at the end of
  validation. This is the defined behaviour (the last executed target starts at 0; design 5.9 in
  `docs/development.md`).
- Most of the loss came at the turn (2025 Q4). Exposure then fell to 9% and the portfolio lost little in the
  decline, but whipsaws in 2026 H1 cost about 6%, and it re-entered late: 8% of BTC's 43% in 2026 Q3.
- All four families lost. The ladder never acted: the drawdown reached −24.0% against the −25% halving level; the
  yellow flag was up 45% of the time.
- Against buy and hold the portfolio lost less (−16% against −29%; drawdown −24% against −54%), but its Sharpe was
  lower (−0.90 against −0.47): the lower exposure cut volatility more than it cut the loss.

## Checks

- Version events: draft → validated (controller) → frozen (human) → failed_final (evaluator), with evidence equal
  to the run's `result.json`.
- Gate 3 applied as approved: Sharpe −0.90 < 0.5 and return −16.1% ≤ 0.
- Buy and hold: BTC 117,341 → 83,576 is −28.8%; with the entry fee, −29.3% as recorded.
- Holdout viewings: `split-1016c7c9873a` 1 of 10 (this run), `split-c47a0be57a24` 0 of 10. No overrides.
- Validation uses: 1 per split. Portfolio dev runs: 1 trial per split.

## Reading

- Over 1.16 years of daily returns the standard error of an annualised Sharpe is about 0.9. A holdout Sharpe of
  −0.90 is about one standard error below zero and about two below the validation Sharpe. One period cannot show
  whether the seed construction has no edge or met an unfavourable regime; the gate does not ask that question, and
  the verdict stands.
- Validation (2024-06 to 2025-08) was mostly a rising market; the holdout held a top, a 54% decline and a sharp
  recovery. A long/cash trend portfolio loses at turns and in ranges and gives up part of sharp recoveries; the
  holdout showed all three.
- The ETH validation failure points the same way: in a range-bound period, trend_filter lost and the other families
  barely covered costs.

## Constraints from here

- Nothing is tuned on these results and re-tested on these holdouts (A6). Both holdouts span the same dates, so
  what the BTC holdout showed also informs ETH.
- The human has seen the holdout path quarter by quarter. A hypothesis motivated by it (exit speed at turns,
  range filters, re-entry speed) is holdout-informed for both current splits. How the remaining viewings of the
  current splits may be used is a decision for the next plan.
- The next split can be cut from 2027-10-12 (A11). Its dev then contains the current validation and holdout, and
  its validation and holdout are data not yet seen.
