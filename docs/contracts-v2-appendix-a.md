# Contracts v2, appendix A: statistics and admission rules for portfolio research

Date: 2026-10-15. Status: approved 2026-10-15, including all eight points of A12, amendments A13.1 to A13.7, and the phase 3 decisions in A14. Amends `docs/contracts-v2.md` (approved 2026-10-12, not edited)
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
- Passive series (as amended by A13.1): not flat, and `sd(ε_w) < 1e-4` per day (about 0.2% a year), where `ε_w`
  is the winsorised residual, also when a report clusters raw residuals: return explained by the coin alone, so
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

## A12. Points decided here that the design left open (all approved 2026-10-15)

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

## A13. Amendments

Amendments change the rule text above only once approved; until then the approved text applies.

### A13.1 Passive series measured on the winsorised residual (approved 2026-10-15)

**Problem.** Under A2 a buy-and-hold series is not passive when its first day deviates from the coin enough. The
residual of holding the coin is almost entirely one day, the entry day (fee, slippage and entry at the first bar's
close instead of its open), and that single day sets `sd(ε) ≈ |ε_first| / sqrt(T)`. On the 2026-10-15 copy of
production data the first-day residual is 0.0092 on BTC and 0.0035 on ETH, so the same strategy measures
`sd(ε)` = 2.6e-4 on BTC (not passive) and 9.9e-5 on ETH (passive). The 37 BTC buy-and-hold trials then form an
ordinary cluster, which at `ρ*` = 0.3 is mapped to `vol_overlay` by session tags, and they would enter that
family's archive.

**Amended rule (replaces the passive bullet of A2).**

- Passive series: not flat, and `sd(ε_w) < 1e-4` per day, where `ε_w` is the residual after the A2 winsorising
  (clipped once at `mean(ε) ± 3·sd(ε)`). The passive set is computed from `ε_w` also when a report clusters raw
  residuals, so raw and winsorised K differ only in the correlation input. All passive trials form one cluster.
- Property, stated so the rule is understood: for k isolated days that deviate from holding the coin by about `a`
  each (and are clipped), `sd(ε_w) ≈ 3·k·a / T`. With T = 1,267, a series is passive when its few deviations add up
  to less than about 4.2% over the dev window (one day under 4.2%, two days under 2.1% each). Persistent
  deviations (timing, partial exposure) are not clipped away and stay far above the threshold.
- The threshold (1e-4), the flat rule, winsorising and A3 are unchanged.

**Effect on the 2026-10-15 copy of production data.**

| | BTC | ETH |
|---|---|---|
| Passive trials, A2 as approved | 0 | 34 |
| Passive trials, amended | 37 | 38 |
| Largest `sd(ε_w)` among passive / smallest among the rest | 6.2e-5 / 4.9e-4 | 5.3e-5 / 2.8e-4 |
| `K(0.3)`, `K(0.5)`, `K(0.7)` as approved | 41, 100, 232 | 41, 99, 241 |
| `K(0.3)`, `K(0.5)`, `K(0.7)` amended | 42, 100, 231 | 40, 98, 240 |

- Every newly passive trial has `|β − 1| < 0.001` and at most two days with `|ε| > 0.1%`; no trial lies between
  the passive group and the rest (the nearest is 5 to 8 times the threshold), so the rule does not hinge on the
  exact threshold.
- The BTC cluster mapped to `vol_overlay` at 0.3 (38 trials) keeps one non-passive trial; the ETH cluster mapped to
  `vol_overlay` (4 trials, all buy and hold) disappears. Flat and passive trials map to no family, so neither
  family archive contains buy-and-hold trials any more.
- One near-buy-and-hold trial stays non-passive (ETH `exp-2e7c9b46a79a`, β 0.997, three deviating days,
  `sd(ε_w)` = 2.8e-4): it trades, and it remains an ordinary trial.

**Alternative considered: drop the first day.** Refitting without day 1 gives nearly the same set (BTC 36, ETH
38) but misses a BTC buy-and-hold variant that deviates on two days, and it only handles the entry day, not an
entry delayed by a warm-up. Rejected in favour of the winsorised measure, which A2 already defines.

**Implementation.** `memory/residuals.py` computes `residual_sd` and the passive flag from `ε_w`; the clustering
input hash includes a rule version, so the next `zqr memory cluster` records a new clustering for every split even
though the returns did not change (earlier clusterings stay on record); the return index flags follow the latest
clustering. Tests: a buy-and-hold series with a large first-day residual is passive; a series out of the coin for
5% of days is not; the raw-residual K uses the same passive set.

### A13.2 Shared clusters when seed families tie (approved 2026-10-15)

**Problem.** A12.4 maps a cluster with seeds to the seeds' family but does not say what happens when seeds of
several families are equally many. The implementation broke the tie by the lowest experiment id among the seeds;
seed experiment ids are random, so the same data gave `ewma_trend` on the local copy and `tsmom` in production for
the same BTC cluster (454 trials at `ρ*` = 0.3; seeds: `ewma_trend` 3, `tsmom` 3, `trend_filter` 2,
`channel_breakout` 2). Either choice leaves the other family's BTC archive empty although the cluster is equally
its history: long/cash trend-following variants share one coin's bull and bear phases and correlate at this
threshold.

**Amended rule (adds to A12.4).**

- A cluster with seeds maps to the family with the most seeds in it. When several families tie for the most seeds,
  the cluster maps to all of them and is a **shared cluster**. No experiment id or name decides.
- The tagged-majority rule for clusters without seeds is unchanged (a strict majority cannot tie).
- Family archives include a shared cluster in each of its families with `is_shared: true` and the list of the
  other families (`shared_with`). Each family archive reports its trials in total and how many of them are in
  shared clusters. Totals across families (the clustering report's trials by family) count a shared cluster under
  each family and also give the deduplicated number of mapped trials.
- A conclusion without a family of its own (A12.4: topics `trend`, `multi_timeframe`) goes to the families with
  the most cited experiments; a cited experiment in a shared cluster counts for each of its families, and a tie
  attaches the conclusion to every tied family.
- K, the proposals (only unmapped clusters become proposals) and the mapping threshold are unchanged.

**Effect on the 2026-10-15 production data.** One cluster is shared: the BTC trend cluster above (454 trials),
mapped to `ewma_trend` and `tsmom`. Every other cluster with seeds has a single top family (for example the ETH
cluster with `ewma_trend` 3, `trend_filter` 2, `tsmom` 2 stays `ewma_trend`). BTC archives then hold 454 trials for
`ewma_trend` (all shared) and 456 for `tsmom` (454 shared); deduplicated, the mapped BTC trials are 784 of 891
with returns, as now.

**Implementation.** `map_clusters` returns a list of families and a `shared` flag per cluster; the archives, the
report and the conclusion assignment use the list. Tests: a seed tie maps to both families and marks the cluster
shared, independent of experiment ids (the same returns under permuted ids give the same mapping); both archives
list the cluster with `is_shared`; the report's deduplicated total equals the mapped trials. After release:
`zqr memory cluster` (the mapping is recomputed from the recorded clustering; K does not change) and
`zqr memory archive --sandbox docker`.

### A13.3 Closing a holdout that the human has seen (approved 2026-10-15)

**Problem.** A9 allows ten holdout viewings per split, one portfolio version per split every 7 days. On
2026-10-15 the BTC seed portfolio failed gate 3 on its first viewing (`docs/reviews/2026-10-15-seed-portfolio-review.md`),
and the human saw the holdout path quarter by quarter. The BTC and ETH holdouts cover the same dates. Any later
version on either current split would be built by people who know that path, so a viewing would test a
hypothesis on the data that suggested it; the viewing count would still show an untouched test.

**Amended rule (adds to A9).**

- The human can close a split's holdout: `zqr close-holdout --split <id> --note <reason>`. The closure is
  permanent and recorded (`holdout_closures`: split, note, time); closing twice keeps the first record.
- A closed split refuses every new holdout viewing, of portfolio versions and of single candidates (the desk's
  standing authorization stops before freezing). A repeat of an earlier viewing still returns its recorded result.
- `--override-reason` lifts the closure as it lifts the cap: the viewing is recorded, the override too, and the
  split's holdout is marked contaminated.
- A closure is not a contamination: no unrecorded viewing happened. The report marks the split "closed".
- Gate 3 resumes on the next split (A11), whose holdout is data after 2026-10-13 06:00 UTC.

**Applied on release:** `split-1016c7c9873a` (BTC, 1 viewing) and `split-c47a0be57a24` (ETH, 0 viewings).

**Implementation.** `ExperimentStore.close_holdout` / `holdout_closure`; `validation/holdout.py` checks the closure
in `run_holdout` and `run_portfolio_holdout` before the cap; `desk/promotion.py` stops before freezing. Tests: a
closed split refuses a new candidate and a frozen version without loading holdout data, answers a repeat, and
records an override as contamination; promotion stops on a closed split.

### A13.4 Splits that extend an earlier split's dev backwards (approved 2026-10-15)

**Problem.** A split owns its trials, K and holdout viewings (A11). The longer-dev decision (E1 in
`docs/superpowers/plans/2026-10-15-phase4-campaigns.md`) creates splits whose dev contains the whole dev of an
earlier split (2021-01-01 to 2024-06-20 18:00 UTC) and whose validation and holdout are the same windows. Every
idea tested on the earlier split was shaped by that data; counting trials from zero on the new split would report
a search that did not happen.

**Amended rule (adds to A3, A9 and A11).**

- A split may name a parent: `zqr split-lineage --split <child> --parent <parent> --note <reason>` (human),
  recorded once (`split_lineage`: child, parent, note, time). Allowed only when both splits are for the same
  product and granularity, the child's validation and holdout windows equal the parent's, the child's dev contains
  the parent's dev, and the child's dataset extends the parent's (the parent's bars copied verbatim,
  `zqr extend-dataset`).
- The child's K at each threshold is its own clustering's K plus its ancestors' (each ancestor's latest
  clustering); `K(0.7)` so obtained enters every calculation on the child (A4, A5). The child's trial count for
  the legacy DSR (contracts v2 section 6) is likewise its own plus its ancestors'.
- The child inherits its ancestors' holdout closures (A13.3): a holdout request on the child is refused when any
  ancestor is closed, whichever was recorded first; recording the lineage under a closed parent also records the
  child's own closure, with a note naming the parent's. Holdout viewings and validation uses stay per split; the
  report shows the parent's next to the child's.
- Reports show K and trials as "own + inherited (parent)". Clustering runs per split as before; the parent's trials
  are not re-run or re-clustered on the child.
- Seed characterisation runs (A7) and the seed portfolio's dev run are repeated on the child. Both splits' seed
  trials then count on the child (at most 12 + 1 per product twice); conservative, accepted for simplicity.
- The A4 reference values for the long dev (T = 3,258 BTC from 2015-07-21, 2,942 ETH from 2016-06-01; the start
  dates exclude the first partial UTC day of BTC and the intermittent ETH listing fortnight) are in the plan.

**Applied:** the E1 splits of BTC-USD and ETH-USD, parents `split-1016c7c9873a` and `split-c47a0be57a24`.

**Implementation.** `ExperimentStore.record_lineage` / `split_parent` / `split_ancestors` / `split_trial_count`;
`validation/splits.py` `create_split_like`, `link_split`; `memory/clusters.py` `k_breakdown`, `effective_k`,
`k_label`; the legacy DSR in `experiments/runner.py` and `validation/walkforward.py`; `data/snapshot.py`
`extend_dataset`. Tests: an extended dataset keeps the base rows' hash and refuses gaps, overlaps and mismatches;
lineage refused on different windows or data; K and trial counts add the ancestors', recursively; a child of a
closed split refuses a holdout without loading holdout data.

### A13.5 Isolated bad prints in candle data (approved 2026-10-15)

**Problem.** The E1 rehearsal on a copy of production data found three bars in Coinbase's pre-2021 hourly candles
at prices no order of any size could have traded at:

- BTC-USD 2017-04-15 23:00 UTC: open and low 0.06, close 1,178.85, previous close 1,182.99 (the first bar after an
  exchange outage).
- ETH-USD 2017-04-15 23:00 UTC: open and high 74.98, close 48.94, previous close 48.53 (the same outage).
- ETH-USD 2017-06-21 19:00 UTC: low 0.10, open 315.97, close 298.00 (the GDAX flash crash, a few seconds long).

The engine fills at the next bar's open, so seeds that entered at the BTC open show returns of 20,000x and more;
Turtle's channels read highs and lows. `validate_bars` checks that prices are positive and OHLC-consistent; these
bars pass.

**Amended rule (adds to the data validation of contracts v2).**

- `validate_bars` drops isolated prints as invalid reason `isolated_print`:
  - an isolated open: the open is more than 20% away from both the previous close and the bar's own close;
  - an isolated wick: the low is below half of the smallest of open, close and previous close, or the high is above
    twice the largest.
  The previous close is that of the previous valid bar, whatever the gap. The first bar of a request has no previous
  close: the open check is skipped and the wick check uses its open and close.
- A dropped bar becomes a one-bar gap, and the dataset's fill policy treats it like any other gap (`ffill_flat`: a
  flat bar at the previous close, volume 0). The validation report lists each dropped bar with its time and prices.
- Thresholds: in 24/7 trading a bar opens at the previous close. Over nine years of both products the largest
  isolated-open deviation of any other bar is 5.6% (ETH, June 2016, the listing weeks; BTC 1.2%), and the largest
  other wick ratio is 1.41 (ETH 2017-03-17). Both thresholds are three times or more beyond any genuine bar, and
  flag only the three bars above.
- Applies to every dataset frozen or extended after release 4b. Frozen datasets are immutable and unchanged; the
  rule flags no bar in their range (2021-01-01 to 2026-10-13 06:00 UTC, both products). The paper bar store is not
  affected.

**Implementation.** `data/validate.py` `validate_bars`. Tests: each kind dropped and reported; a genuine large move
(a 25% hourly fall from an open at the previous close) kept; the fill policy fills the dropped bar.

### A13.6 The ladder in research measures (approved 2026-10-15, including gate 2)

**Problem.** A14.1 runs the drawdown ladder in every backtest. After a liquidation (−30%) the ladder waits for a
human resume, which no backtest has, so the run stays in cash to the end of the segment. On the long dev (A13.4)
the rehearsal's seed portfolios liquidated on 2020-03-12 (BTC; 48% of dev in cash) and 2017-09-15 (ETH; 84%). As
gate 1's `R_p` that series is zero for most of dev, and every variant would be regressed on cash. A14.1's argument
that the ladder does not change the seed verdict holds for a validation segment, not for a nine-year dev.

**Amended rule (amends A6 and A14.1).**

- Measures of a portfolio's construction use the version run without the ladder (same members, weights, overlays
  and costs; the ladder levels disabled):
  - gate 1's `R_p` (A6);
  - the portfolio dev run's series in the return archive (A1), and so in clustering (A3);
  - campaign diagnostics and period statistics, which show both runs side by side.
- Gate 2 (validation) runs both portfolios, with and without the variant, without the ladder: the ladder caps both
  drawdowns near −30% and freezes both runs after a liquidation, so the drawdown and Sharpe comparisons would
  compare ladder timing rather than the variant.
- The ladder stays wherever the version is judged as it would trade: the seed validation standard (A6), gate 3
  (holdout), paper and live. Reports keep both drawdowns.
- No resume rule is simulated: a rule for when a human would resume is a new parameter, chosen on the dev it would
  then be judged on.
- Applies to portfolio runs recorded after release 4b; earlier runs stay as recorded. On the 2021-2024 splits the
  ladder never liquidated (the ETH seed portfolio spent 7% of one run halved).

**Implementation.** `portfolio/evaluate.py`: the dev run writes the run without the ladder next to the run with it
(`equity_no_ladder.parquet`); the archive and `period_stats` read it; gate 1 and gate 2 read it when they are built
(phase 4 Tasks 3-16). Tests: a dev run that liquidates archives the series without the ladder; periods show both.

### A13.7 How the gates are run (approved 2026-10-15 with the phase 4 plan, releases 4c and 4d)

**Problem.** A6 states the gates; it leaves open which portfolio is the reference, how the candidate portfolio is
built, how validation uses are counted for the reference, and what else gate 1 reports.

**Rule (details A6; uses A13.6).**

- **Reference.** A hypothesis is registered on its product's desk mandate split; gate 1's reference (D4, recorded
  by `zqr portfolio dev <vid> --reference`) is fixed at registration and recorded with it. Both gates use it,
  even if the mandate changes later.
- **Scope.** Variants are sleeve members (template or plugin) of one family on one product. A different overlay
  changes the reference itself and is not registered as a hypothesis.
- **Gate 1.** `R_c` is the variant alone on the full dev of the split, with the reference's engine configuration
  (costs, cash, long only); `R_p` is the reference's dev run without the ladder; both on the A1 day grid, common
  days only. Each variant run is a trial (kind `registered`). Ties are broken by A6 (highest AR).
- **Gate 1 diagnostic.** The hedged series' Sharpe is also recorded per period (the cuts of the long-dev review:
  before 2021 and 2021 onwards). It is shown to the human and, being dev data, may be shown to agents. It never
  changes the verdict.
- **Gate 2.** The candidate version is the reference's members and overlay plus the variant as a member at full
  weight (status `member`, a new library entry with status `candidate`), blended by the normal rules (a new family
  is a new sleeve); parent = reference. Both versions run on validation without the ladder. Pass only if the
  candidate's A4 Sharpe ≥ the reference's, its maximum drawdown ≥ the reference's − 0.02, and its turnover ≤ 1.25
  × the reference's.
- **Validation uses.** The reference's validation run is recorded once per split and counted once; every
  candidate's validation run is counted.
- **Outcome.** A passing candidate version goes from `draft` to `admitted` (controller); a failing one to
  `failed_validation`. `admitted` versions wait for the next split (design D5); they cannot be frozen, approved
  or shadowed on the split where they were admitted.

**Caveat.** The human has seen the seed portfolios' validation results (the seed review), so on the current
splits gate 2 is a harm check, not independent evidence; the next split's holdout is the independent test.

## A14. Portfolio layer decisions (approved 2026-10-15)

Decided with the phase 3 plan (`docs/superpowers/plans/2026-10-15-phase3-portfolio.md`, D4-D7); the investment
policy records D1-D3.

1. **The drawdown ladder is part of every portfolio version** and runs in dev, validation and holdout backtests and
   in paper through one function (`portfolio/decide.py`). It does not change the seed validation verdict: the
   ladder acts only at −25% or worse, so a portfolio whose drawdown never reaches −25% runs identically with or
   without it, and one that reaches −25% fails the A6 standard either way. Reports also show the drawdown without
   the ladder.
2. **Portfolio dev runs count as trials.** Each version's dev run is an experiment of kind `portfolio`, counted on
   the split and added to the return archive without a family tag (consistent with A8: a construction change
   counts as a trial). Validation runs (kind `portfolio_validation`) and holdout runs (kind `holdout`) are not
   trials; each validation run is counted per split (`validation_uses`) and shown in the report (A6).
3. **Version transitions after validation are human-only** in phase 3: freeze, holdout and approval to paper. The
   standing authorization covers single candidates, not portfolio versions.
4. **Gate statistics use A4.** Sharpe in the seed validation standard and in gate 3 is the A4 daily Sharpe of the
   version's daily returns (A1 day grid); maximum drawdown is measured on bar-close equity; total return is final
   over initial equity.
