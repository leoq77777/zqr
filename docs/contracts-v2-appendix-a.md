# Contracts v2, appendix A: statistics and admission rules for portfolio research

Date: 2026-10-15. Status: draft for human review. Amends `docs/contracts-v2.md` (approved 2026-10-12, not edited)
for portfolio research, as decided in `docs/superpowers/specs/2026-10-13-research-library-portfolio-design.md`
(sections 4.4, 5.3, 5.4, 5.5, 5.6 and 6.5). Takes effect for a mandate only when the mandate version names it
(`statistics: appendix-a-v1`); mandates already stored are immutable and keep their rules.

Section A12 lists the points where this appendix decides something the design left open; each needs explicit
approval.

## A0. What changes and what does not

| Area | Contracts v2 | This appendix (portfolio research) | Implemented in |
|---|---|---|---|
| Multiple-testing measure | DSR per experiment: K = raw trial count on the split, luck dispersion = variance of trial Sharpe ratios, hourly returns | Split DSR: K = effective number of independent trials from clustering, luck dispersion from the null (`σ0`), daily returns; reported, not a gate | Phase 2 |
| Acceptance | `min_dsr` in the mandate (section 6) | Registered test on the appraisal ratio, K = registered variants (at most 3), plus do-no-harm on validation and the holdout | Statistic phase 2, gates phase 3 |
| Rebalancing | `rebalance_tolerance` against the drifted weight (section 4) | Signal deadband against the last executed target, portfolio path only | Phase 3 |
| Holdout | Viewing limit per mandate (section 7) | 10 viewings per split, one per portfolio version, at most one version per 7 days | Phase 3 |
| Paper admission | `paper_approved` by the human | Probation at half share for 90 days with predeclared exit rules | Phase 3 |
| Splits | Fixed split per dataset | Predefined schedule for the next split | Phase 3 report, cut by the human |

Unchanged: contracts v2 sections 2-5 (timestamps, exposure contract, accounting, portfolio policy), 8-10
(protected data, gateway, adapters). The single-candidate flow keeps its `min_dsr` gate for controls and
benchmarks. The per-experiment `statistics.dsr` already recorded stays as recorded; reports that show it label it
"legacy DSR (hourly, empirical variance)".

## A1. Daily return archive

Every completed trial on the dev segment is reduced to daily returns.

- Trial: an experiment counted by `ExperimentStore.trial_count` (status not `blocked`, kind not in `holdout`,
  `sensitivity`, `walkforward_fold`), on the split's dev segment.
- Day: the UTC calendar date of the bar's open time. A day's equity is the equity marked at the close of its last
  bar (the 23:00 bar closes at midnight). Returns: `r_d = E_d / E_{d-1} - 1`, with `E_{-1}` the initial cash.
- The dev window of both current splits is 2021-01-01 00:00 to 2024-06-20 18:00 UTC (end exclusive). Its last day
  is partial (18 bars) and is kept: T = 1,267 daily returns for every trial on a split.
- Stored per split in `ZQR_HOME/library/returns/<split_id>.parquet` (one column per experiment, indexed by day)
  with an index table in `experiments.db` (experiment, split, family tag, Sharpe, turnover, number of days, flags).
- Trials that ended `failed` or `rejected` have no returns; they are listed in the index with `no_returns = 1`.

## A2. Market component and special series

- Market return `m_d`: the product's close-to-close return on the same day grid (close of the day's last bar over
  the previous day's; the first day over the first bar's open).
- Residuals: ordinary least squares with intercept, `r_d = a + b·m_d + ε_d`.
- Winsorising: `ε` is clipped once at `mean(ε) ± 3·sd(ε)` (sample standard deviation).
- Flat series: `max|r_d| < 1e-12` (never trades). All flat trials form one cluster.
- Passive series: not flat, and `sd(ε) < 1e-4` per day (about 0.2% a year): return explained by the coin alone, so
  residual correlations are noise. All passive trials form one cluster.

## A3. Clustering and the effective number of trials

- Pearson correlation `ρ` of the winsorised residuals of the remaining trials; distance `sqrt((1 - ρ) / 2)`.
- Average-linkage hierarchical clustering (`scipy.cluster.hierarchy.linkage`, method `average`) over trials
  ordered by experiment id; clusters cut at distance `sqrt((1 - ρ*) / 2)` for `ρ*` in {0.3, 0.5, 0.7}.
- Cluster labels are canonical: each cluster is named by its smallest experiment id, so a recomputation on the
  same inputs gives identical labels. Clusterings are recorded with the hash of their inputs.
- Effective number of trials on a split at threshold `ρ*`:
  `K(ρ*) = clusters + [any flat trial] + [any passive trial] + trials with no returns`.
- K is reported at all three thresholds; `K(0.7)` (the largest) is used wherever K enters a calculation.
- Recomputed in full after each batch of trials. K is a property of the split's history: it is never reset and,
  as trials accrue, it is expected to grow.

## A4. Sharpe statistics on daily returns

For a daily return series `r` of length T (all values per day unless stated):

- Annualised Sharpe `SR = mean(r) / sd(r) · sqrt(365)`, sample standard deviation; `SR = 0` when `sd(r) = 0`.
  Per-day Sharpe `s = SR / sqrt(365)`.
- Skewness `γ3` and kurtosis `γ4` (not excess): population moments of the standardised series.
- Standard error under the null: `σ0 = sqrt(365 / (T - 1))`.
- Standard error at the estimate: `σ_SR = sqrt((1 - γ3·s + (γ4 - 1)/4 · s²) / (T - 1)) · sqrt(365)`; when the
  term under the root is not positive, `σ_SR = σ0`.
- Denominator `σ = max(σ0, σ_SR)`: fat tails may widen it, never narrow it below the null.
- Expected maximum Sharpe of K skill-less trials:
  `SR0(K) = σ0 · ((1 - γ)·Φ⁻¹(1 - 1/K) + γ·Φ⁻¹(1 - 1/(K·e)))`, `γ` = 0.5772156649 (Euler-Mascheroni);
  `SR0(1) = 0`. The dispersion is the null's, not the spread of trial Sharpe ratios, which is dominated by trials
  that lose deterministically to costs.
- `DSR(K) = Φ((SR - SR0(K)) / σ)`.

Reference values for T = 1,267 (`σ0` = 0.5369); "needed" is the annual Sharpe giving `DSR = 0.9` when `σ = σ0`:

| K | 1 | 2 | 3 | 20 | 110 | 260 |
|---|---|---|---|---|---|---|
| `SR0(K)` | 0 | 0.279 | 0.458 | 1.021 | 1.377 | 1.530 |
| Needed | 0.688 | 0.967 | 1.146 | 1.709 | 2.065 | 2.219 |

These are fixed-input test values for the implementation.

## A5. Split DSR (reported, not a gate)

- `split DSR = DSR(K(0.7))` on the dev daily returns, with K from the split's latest clustering.
- Reported for every candidate, library member and family representative, in the daily report, `zqr library show`
  and the family archives, together with K at the three thresholds.
- It measures how much the split has been searched. It does not admit or reject anything. Portfolio research
  mandates set `min_dsr = null` with `dsr_waiver: "appendix A: split DSR reported; admission by the registered
  test (A6)"`.

## A6. Registered test and admission gates

**Registration.** A hypothesis is registered once, before the controller tests it, and is immutable: a written
prior (a citation to published work, or archive evidence with experiment ids), and one to three variants with fixed
code hash and parameters. More than three variants, or a second registration in the same campaign, is refused.

**Gate 1, marginal value (dev).** `R_p`: daily dev returns of the current portfolio version at full weights (the
seed portfolio until another version is frozen). `R_c`: the variant's daily dev returns on the same days.

- OLS `R_c = α + β·R_p + ε`; hedged series `h = R_c - β·R_p` (so `mean(h) = α`, `sd(h) = sd(ε)`).
- Appraisal ratio `AR = SR(h)` (A4 formula).
- Statistic `Φ((AR - SR0(K)) / σ_h)` with K = number of registered variants and `σ_h = max(σ0, σ_SR(h))`.
- Pass: statistic ≥ 0.9, i.e. `AR` ≥ 0.688 for one variant, 0.967 for two, 1.146 for three (when `σ_h = σ0`).
- If several variants pass, the one with the highest AR goes to gate 2. Each marginal evaluation counts as a trial.

**Gate 2, do no harm (validation).** The portfolio with the variant at full weight against the portfolio without
it, on the validation segment. Pass only if all three hold: Sharpe not lower; maximum drawdown no more than 2
percentage points worse; turnover no more than 25% higher. The agent sees only the verdict (passed, or which gate
failed), never validation metrics. Each use of validation is counted per split and shown in the report.

**Gate 3, final (holdout).** The new portfolio version as a whole at full weights: Sharpe ≥ 0.5 and total return
> 0. One holdout viewing per version (A8).

**Seed portfolio.** Seeds are admitted on their published evidence without gate 1. The seed portfolio's validation
standard, fixed before validation is looked at: Sharpe ≥ 0.5 and maximum drawdown no worse than −25% (the
investment policy's halving level). A failure is recorded and discussed; the portfolio is not adjusted and retried
automatically.

## A7. Seed characterisation runs

Each seed instance (12 entries × 2 products) runs once on dev as an experiment of kind `seed`, to characterise
families and to anchor the clustering. They count as trials on the split (K and trial counts include them). They
do not select seeds: the seed portfolio includes every reviewed seed regardless of these results.

## A8. Signal deadband (portfolio path)

An order is generated only when the blended target differs from the last executed target by at least the band, or
when the target is 0 and a position is open (full exits always execute). Price drift alone never trades. The band
is fixed when the seed portfolio is defined (design value 0.10); changing it later counts as a trial. Backtest and
paper use one shared function. The single-strategy path keeps the engine's `rebalance_tolerance` (contracts v2
section 4).

## A9. Holdout budget

Each split has 10 holdout viewings. Each portfolio version uses one; accepted changes are batched, at most one new
version per split every 7 days. Every holdout path goes through the single capped gate (`run_holdout`, phase 0); a
manual override needs a flag and a reason and marks the split's holdout as contaminated. The report shows viewings
used and left.

## A10. Probation

A new member enters paper at half its share; the freed half stays in cash. The version records the start date.
After 90 days of paper it becomes a full member, unless:

- decision parity failed for the portfolio on a day the member contributed to the target (any rule mismatch:
  target, order side or order size; allowed differences do not count), or
- its paper residual is negative beyond one standard error: the member's shadow daily returns (its target series
  on the paper bars through the backtest engine, same costs) regressed on the shadow returns of the rest of the
  portfolio give `α / se(α) < −1`.

A demoted member is retired with the reason archived. Promotion and demotion are predeclared in the version and do
not use a holdout viewing.

## A11. Split schedule

The next split is cut when the data after the current holdout's end (2026-10-13 06:00 UTC) covers at least
182 days of validation and 182 days of holdout; earliest 2027-10-12. Segments of the new split: validation and
holdout are the last 2 × 182 days, in that order; dev is everything before, so the current validation and holdout
fold into dev. Trials, K and holdout viewings belong to a split; the archives carry the earlier splits' conclusions
forward. The daily report shows the earliest cut date.

## A12. Points decided here that the design left open (approve or amend each)

1. **Trials without returns** (`failed`, `rejected`) each add 1 to K (A3). Conservative; currently 5 BTC and 2 ETH
   trials.
2. **Passive cluster** (A2): series whose residual is below 1e-4 a day are grouped once, like flat series, instead of
   entering the correlation matrix as noise.
3. **Seed characterisation runs count as trials** (A7): K and trial counts include them (+12 per split).
4. **Family mapping threshold**: clusters map to families at `ρ*` = 0.3 (the coarsest cut, 34 to 43 clusters per split
   in the 2026-10-13 feasibility run), while K uses 0.7. Old trials are tagged from their session topic:
   `volatility_filter` → `vol_overlay`; `trend` and `multi_timeframe` → no family tag (placed by the seeds in their
   cluster); `mean_reversion` → a proposed family `short_term_reversal`, archive only, recorded as infeasible under
   costs (no sleeve). A cluster with no seed and no tagged majority becomes a new-family proposal for the human.
5. **Holdout batching** (A9): at most one portfolio version per split every 7 days.
6. **Probation residual test** (A10): `α / se(α) < −1` on the member's shadow returns against the rest of the
   portfolio over the 90 days.
7. **Seed portfolio validation standard** (A6): Sharpe ≥ 0.5 and maximum drawdown no worse than −25%.
8. **Split schedule segment lengths** (A11): 182 days of validation and 182 days of holdout.
