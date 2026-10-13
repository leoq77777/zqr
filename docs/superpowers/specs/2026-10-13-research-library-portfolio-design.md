# Design: strategy library, research memory, portfolio and campaigns

Status: approved in discussion on 2026-10-13, revised after review the same day, pending review of this document.
Supersedes the desk's topic-rotation research loop (`desk/research.py`) as the way research is directed. Rule
changes in sections 4.4, 5.3, 5.4 and 5.6 take effect only after the human approves the contract appendix.

## 1. Problem

The desk ran 177 research sessions in 10 hours (2026-10-13, deepseek-flash, $6) and produced no candidate. The
sessions were independent: each started from scratch, rotated through four topics and wrote a fresh plugin, so
nothing accumulated. About 1,780 dev trials on two splits pushed the deflated-Sharpe threshold to an annual Sharpe
of about 7, which no strategy can reach.

Three causes:

1. **Research process.** Brute-force search without memory or direction. Every trial raises the bar for every
   later trial.
2. **DSR estimate.** The luck dispersion is taken from the variance of Sharpe ratios across trials. That variance
   is dominated by trials that lose deterministically to costs (cluster Sharpe down to −11), not by luck.
3. **Sample size.** The dev segment is 3.47 years of daily returns. The standard error of an annual Sharpe estimate
   is about 0.54 even for a single test, so after many trials our own data cannot confirm a strategy with an annual
   Sharpe near 1 (section 4.4).

The research question itself is hard: hourly bars, long/cash only, 0.6% taker fee per side (about 1.3% per round
trip).

## 2. Decisions

| Topic | Decision |
|---|---|
| Final output | One blended position per product (BTC-USD, ETH-USD). Paper trades the blend; the 16 single-strategy paper traders stay as controls |
| Costs | Unchanged: 0.6% taker per side plus 5 bps slippage |
| Seed strategies | Curated during development from published sources, written as deterministic templates, reviewed by the human before import |
| Research agent | Works on campaigns generated from portfolio gaps, each with a trial budget; idles when the queue is empty |
| Existing trials | Splits kept; old trials count in full, reduced to an effective number of independent trials by clustering; validation and holdout stay unused |
| Admission of new members | Prior first: a written prior and a registered hypothesis of at most three variants; tested on dev with K equal to the registered variants; enters paper at half weight on probation. The DSR over all trials on the split is reported, not used as a gate |
| Architecture | Layered: strategy library, research memory (returns, clusters, family archives), portfolio layer, campaigns |

Universe stays BTC-USD and ETH-USD spot, long/cash. Live trading stays out of scope.

## 3. Strategy library and seeds

### 3.1 Concepts

| Concept | Content | Written by |
|---|---|---|
| Library entry | Template or frozen plugin with fixed parameters; source (publication, seed, campaign); family; rationale; adaptation notes; status `candidate` → `probation` → `member` → `retired` | Seeds: human review and import. Campaign results: controller, after the portfolio gates |
| Family | Named research direction (e.g. medium-term trend) with a thesis and an archive (section 4.6) | Seed families: human. New families: proposed by clustering, confirmed by the human |
| Portfolio | Per product: members, blend rule, no-trade band, volatility overlay; immutable versions (section 5) | Controller only |
| Campaign | Goal, target family, budget, registered hypothesis, conclusion (section 6) | Generator; the human can add or cancel |

Stored in SQLite and `ZQR_HOME`, managed by deterministic code. The agent reads them through gateway tools only.

### 3.2 Seed catalog

`library/seeds.yaml` plus a readable `docs/library/seeds.md` (sources, logic, parameter translation). Imported with
`zqr library import` after review. Only low-turnover families, because of costs:

| Family | Sources | Form here |
|---|---|---|
| Time-series momentum | Moskowitz, Ooi & Pedersen (2012); Hurst, Ooi & Pedersen, century of trend following (AQR); Liu & Tsyvinski (2021), 1–4 week crypto momentum | Long when the trailing 1-week, 1-month or 12-month return is positive; a continuous variant scales the position with signal strength |
| Moving-average trend | Brock, Lakonishok & LeBaron (1992); Baz et al. (2015), EWMA crossovers | Several fast/slow EWMA pairs, volatility-scaled to a 0–1 position |
| Channel breakout | Turtle rules (Faith, *Way of the Turtle*); Donchian channels | 20/55-day entry breakout, 10/20-day exit |
| Trend filter | Faber (2007), 10-month moving average | Long above the long moving average |
| Volatility management | Moreira & Muir (2017); Harvey et al. (2018) | Portfolio overlay: exposure scaled inversely to realised volatility, capped at 1 |

Liu & Tsyvinski's sample (2011–2018) does not overlap our data (from 2021), so it is an out-of-sample prior.
Short-term reversal is documented but cannot survive 0.6% per side; it is recorded in the archive as infeasible
under costs and not seeded.

Every family that takes a sleeve has at least two variants, so no seed member exceeds the single-member cap
(section 5.2).

### 3.3 Priors, not selection

- Canonical parameters from the sources. No tuning. Two or three preregistered variants per family, about 15
  entries.
- Parameter translation to hourly bars follows calendar time, because crypto trades every day: 1 week = 168 bars,
  1 month = 720 bars, 12 months = 8,760 bars. Sources that define a lookback as a count of daily bars (Donchian and
  Turtle channels) keep the count, as crypto daily bars are calendar days: 20 days = 480 bars. Each translation is
  written in the catalog.
- The seed portfolio includes every reviewed seed family, decided before any data is looked at. Seeds are admitted
  on their published evidence and do not go through the dev test in section 5.4.
- Seeds run once on dev to characterise families (returns, turnover, correlations) and to seed the clustering, not
  to select.

### 3.4 New templates

Time-series momentum (binary and continuous), EWMA crossover, breakout with an exit channel, long moving-average
filter, volatility overlay. Existing `sma_cross`, `momentum` and `breakout` are reused where they match.

## 4. Research memory

### 4.1 Return archive

Every completed dev experiment's equity curve is reduced to daily returns (1,267 points over 3.47 years) and stored
per split (`ZQR_HOME/library/returns/<split_id>.parquet`, one column per experiment) with an index table
(experiment, split, turnover, Sharpe). The 1,780 existing trials are backfilled; their equity curves exist.

### 4.2 Removing the market component

Long/cash strategies all hold the coin part of the time, so their returns are positively correlated through the
coin. Clustering raw returns put 875 of 879 BTC trials in one cluster (effective N = 3).

Each trial's daily returns are regressed on the product's buy-and-hold daily returns over the same window; the
residuals are winsorised at ±3 standard deviations, then correlated. Trials that never trade (zero variance) form
one "flat" cluster, counted once.

Winsorising guards against a few crash days dominating the correlations. On 2026-10-13 data it barely changed the
cluster count, and dropping the ten most extreme days did not change it either. Rank correlation was rejected: the
many near-zero residuals of mostly-cash strategies create ties that distort it.

### 4.3 Clustering

- Distance `sqrt((1 − ρ) / 2)` on residual correlations; average-linkage hierarchical clustering.
- Cut at fixed residual-correlation thresholds 0.3, 0.5 and 0.7. K is reported at all three; the largest (ρ = 0.7)
  is used wherever K enters a calculation.
- Deterministic; recomputed in full after each batch of trials (seconds for 2,000 trials).
- Adds the `scipy` dependency (about 40 MB in the image).

Feasibility run on 2026-10-13 data (market component removed via the first principal component as a stand-in for
buy and hold), clusters including the flat cluster:

| Threshold ρ | 0.3 | 0.5 | 0.7 |
|---|---|---|---|
| BTC-USD (879 trials) | 34 (winsorised 43) | 113 (116) | 255 (267) |
| ETH-USD (899 trials) | 34 (38) | 108 (110) | 265 (276) |

### 4.4 Statistics: what our data can and cannot show

For a daily return series with annualised Sharpe `SR`, `T` observations, skewness `γ3` and kurtosis `γ4` (per-period
Sharpe `s = SR / sqrt(365)`):

- Standard error under the null (true Sharpe 0): `σ0 = sqrt(365 / (T − 1))`, about 0.537 for dev.
- Standard error at the estimate: `σ_SR = sqrt((1 − γ3·s + (γ4 − 1)/4·s²) / (T − 1)) · sqrt(365)`.
- Denominator: `σ = max(σ0, σ_SR)`. Fat tails may widen it but never shrink it below the null. The best BTC trial
  has skewness 16 and kurtosis 334 (one day dominates), which would shrink `σ_SR` to 0.32 and make the test
  lenient by accident.
- Expected maximum Sharpe of K zero-skill strategies:
  `SR0(K) = σ0 · ((1 − γ)·Φ⁻¹(1 − 1/K) + γ·Φ⁻¹(1 − 1/(K·e)))`, γ the Euler–Mascheroni constant; `SR0(1) = 0`.
- `DSR(K) = Φ((SR − SR0(K)) / σ)`.

Annual Sharpe needed for DSR ≥ 0.9 on dev (`SR0(K) + 1.28·σ0`):

| K | 1 | 3 | ~20 | ~110 | ~260 |
|---|---|---|---|---|---|
| Needed | 0.69 | 1.15 | 1.71 | 2.06 | 2.22 |

K ≈ 260 is the count over every trial on each split. The best existing trials (BTC 1.13, ETH 1.36) give a DSR of
0.29 and 0.45. Dev evidence cannot confirm a strategy after this much search. Long/cash crypto strategies with an
annual Sharpe above 2 after 1.3% round-trip costs are not a realistic target.

The design therefore uses two quantities:

1. **Split DSR** (K over every trial on the split, never reset): reported for every candidate, member and family
   as a measure of how much the split has been searched. Not a gate.
2. **Registered test** (section 5.4): K is the number of variants registered for one hypothesis (at most three).
   Its evidence rests on the prior (published results across markets and decades, or archive evidence), with dev
   used to check that the variant adds to the portfolio and does no harm.

This replaces the acceptance definition in `docs/contracts-v2.md` for portfolio research. It is written as a
contract appendix (`docs/contracts-v2-appendix-a.md`) and takes effect only after human approval, through new mandate
versions.

### 4.5 Clusters and families

- Library entries and campaign trials carry a family tag. Old trials are tagged from their session topic and
  cluster.
- Each cluster maps to the family holding the majority of its tagged members. A cluster matching no family produces
  a new-family proposal (member parameters, code summary, statistics); the human confirms it.
- Cluster ids are internal and may change between recomputations; families are the stable, human-facing unit.

### 4.6 Family archives

One per family.

Deterministic part, regenerated after each clustering:

- members and the representative per product, with dev statistics and split DSR;
- parameter ranges tried, trials and clusters used, registered hypotheses and their outcomes;
- cost sensitivity (Sharpe at 1× and 2× cost);
- behaviour by regime (trend/range, high/low volatility);
- correlation with other families;
- failure classes; holdout viewings used.

Conclusions, append-only:

- one record per campaign: hypothesis, work done, result, lessons, open questions, session references;
- numbers cited are checked against the experiment records (the existing `research.json` citation check);
- human notes allowed.

Initial archives import the conclusions of the 177 existing sessions.

## 5. Portfolio layer

### 5.1 Definition

Per product: members (library entries) with their status (member or probation), blend rule, no-trade band,
volatility overlay switch. Immutable, hashed, versioned; lifecycle draft → validated → frozen → holdout → paper, like
candidates today.

### 5.2 Blending

Each member outputs a target position in [0, 1] per bar.

1. Within a family: equal-weight average of members.
2. Across families: equal capital sleeves (with F families each uses at most 1/F of capital). Inverse-volatility
   weights would favour families that are mostly in cash, since all trade the same coin.
3. Single-member cap: no member's share of capital exceeds 15%. If a family has too few members, the excess of its
   sleeve stays in cash; it is not redistributed, so the preregistered sleeves hold.
4. Probation: a member on probation counts at half its share; the freed half stays in cash.
5. Volatility overlay (a library entry, subject to the same gates): final = blend × min(1, target vol / realised
   vol).

The same blending code serves backtests and paper trading.

### 5.3 Turnover: signal deadband

Blending smooths switching. On top of it, an order is generated only when the blended target differs from the last
executed target by at least the band (e.g. 0.10), or when the target is 0 and a position is open (full exits always
execute). The band compares targets, not the current market weight: a price rise that lifts the position's share of
equity from 50% to 56% does not trigger a sale. For long/cash the drift stays within [0, 1].

This differs from the engine today: `backtest/engine.py` compares the target with the drifted weight at the fill
bar's open (`rebalance_tolerance`). The portfolio path uses the deadband in backtests and paper alike. The existing
single-strategy path is unchanged.

Band width, volatility target, sleeves and cap are fixed when the seed portfolio is defined; changing them later
counts as a trial.

### 5.4 Admission of new members

A candidate needs a registered hypothesis (section 6.3): a written prior (citation to published work, or to archive
evidence with experiment references) and at most three variants with fixed code and parameters. The controller then
runs the gates.

| Gate | Segment | Test | Standard |
|---|---|---|---|
| Marginal value | dev | Regress the variant's daily returns on the current portfolio's: `R_c = α + β·R_p + ε`. Appraisal ratio `AR = α / σ_ε`, annualised; registered test as in 4.4 with K = registered variants | `Φ((AR − SR0(K)) / σ) ≥ 0.9`, i.e. AR ≥ about 0.69 for one variant, 1.15 for three |
| Do no harm | validation (never shown to the agent) | Portfolio with the variant at full weight vs without | Fixed in advance: Sharpe not lower, max drawdown no more than 2 points worse, turnover no more than 25% higher |
| Final | holdout | New portfolio version as a whole, at full weights | Current rule: Sharpe ≥ 0.5 and return > 0 |

The appraisal ratio is the standard test of whether a return stream extends a portfolio: the best combination's
squared Sharpe equals the portfolio's squared Sharpe plus the squared appraisal ratio. A modest but uncorrelated
strategy can pass; a strong but redundant one cannot. If several variants pass, the one with the highest appraisal
ratio enters. Validation (1.16 years, standard error about 0.93) cannot test significance, so it only checks for
harm.

Literature priors added after the seeds follow the same path, registered by the human or a campaign.

### 5.5 Holdout budget

Each split has 10 holdout viewings; each new portfolio version uses one. Accepted changes are batched into versions
(several accepted changes, or at least a week apart).

### 5.6 Probation

A new member enters paper at half its share (5.2). The version records the probation start. After 90 days of paper
it becomes a full member unless one of these holds:

- decision parity failed for the member;
- its realised paper residual return (against the rest of the portfolio) is below −1 standard error.

In that case it is retired and the reason archived. Promotion and demotion are predeclared in the version, so they
do not use a holdout viewing; the holdout evaluates the version at full weights.

### 5.7 Retirement

Periodic reverse test: remove a member; if the portfolio improves on dev and validation, the system proposes
retirement. In this phase the human confirms. Retired entries stay in the library with the reason in the archive.

### 5.8 Seed portfolio

All reviewed seed families, equal sleeves; members, weights, band, cap and volatility target fixed before
validation or holdout is looked at. No probation for seeds. Validation standard fixed in advance: Sharpe ≥ 0.5 and
max drawdown not worse than −25%. The result is shown to the human before the holdout is used. If it fails
validation, it is recorded and discussed; it is not adjusted and retried automatically.

### 5.9 Paper and report

Paper adds one portfolio trader per product; the 16 single strategies stay as controls. The daily report adds:

- portfolio return against buy and hold;
- contribution by family; members on probation and days remaining;
- current composition, turnover and costs;
- split DSR per member and per split;
- decision parity, bar by bar, as today.

Portfolio diagnostics (drawdowns by regime, concentration, weak periods) feed campaign generation.

## 6. Campaigns

### 6.1 Campaign record

| Field | Example |
|---|---|
| Goal | Reduce medium-term trend drawdowns in range markets without hurting trend markets |
| Target family, product | Medium-term trend; BTC-USD |
| Budget | 30 experiments, 4 sessions, $2 |
| Registered hypothesis | Prior, citations, up to three variants (code hash, parameters); written once, before the controller tests it |
| Status | queued → running → concluded / cancelled |
| Conclusion | Hypothesis, evidence, verdict (supported / refuted / inconclusive), registered variants and gate results, open questions |

### 6.2 Generation

Deterministic, from portfolio diagnostics and archives:

1. regime gaps (drawdowns concentrated in one regime);
2. concentration (one family dominates return or risk) → look for uncorrelated families;
3. cost (a family unprofitable at 2× cost) → lower-turnover variants;
4. open questions from earlier conclusions → follow-up campaigns;
5. exploration (unclaimed clusters, or no new family for a while), a fixed share (about 20%) of campaigns.

Ranked by gap size × room left in the family, within family quotas (6.5). The human can list, cancel or add
campaigns.

### 6.3 Execution

- A campaign is a sequence of sessions with memory. Each session's brief has the goal, the target family archive,
  summaries of other families, notes from earlier sessions of the campaign, and the remaining budget.
- Each session writes progress notes in `research.json`.
- Exploration trials count toward the split's trials and K as today.
- A campaign may register one hypothesis, through a gateway tool. The record is immutable once written. The
  controller runs the gates on the registered variants only.
- The campaign ends when the budget is spent, a hypothesis has been registered and tested, or the agent concludes.
  The last session writes the conclusion; citations are checked; the record is appended to the family archive.

Registering after exploring the same dev data means K = registered variants understates the search. This is
accepted: the prior requirement, the do-no-harm validation check, the holdout, probation in paper, and the split DSR
in the report are the safeguards. The report counts registrations per split and their outcomes.

### 6.4 Gateway tools (dev only)

- family archive query;
- current portfolio members and their dev statistics;
- marginal evaluation on dev (appraisal ratio against the current portfolio); counts as a trial;
- hypothesis registration.

### 6.5 Pace, capacity and cost

- One campaign at a time; idle (no model calls) when the queue is empty.
- Generation after each campaign, after each portfolio version, and daily.
- Daily cap ($20) stays; each campaign has its own cap.
- Family quotas: each family has a trial quota per split. A family that has used its quota gets no more campaigns
  on that split; other families and exploration continue.
- Split schedule, predefined: a new split is cut when unseen data has accrued for at least 6 months of validation
  and 6 months of holdout (from the current split's end, earliest around October 2027). The current validation and
  holdout fold into the new dev. The desk reports the date. Segment lengths are fixed in the contract appendix.

### 6.6 Replacing the topic loop

Topic rotation is removed; the research loop takes the next session from the campaign queue. Gateway, isolation,
audit, cost ledger, standing authorization and recovery stay. The report's research section shows campaigns,
registrations, conclusions, archive changes and budget. Each campaign can name its model, for model comparisons on
like tasks.

## 7. Delivery

### 7.1 Phases

| Phase | Scope | Human checkpoint |
|---|---|---|
| 1. Library and seeds | Templates, library and family schema, seed catalog, `zqr library list/show/import` | Review `docs/library/seeds.md`, then import |
| 2. Memory, clustering, statistics | Return archive and backfill, residual clustering, K at three thresholds, split DSR and registered test, deterministic archives, import of 177 session conclusions | Approve the contract appendix; review the clustering report on old trials |
| 3. Portfolio | Portfolio versions, blending engine with cap, probation and deadband, gates, seed portfolio, paper portfolio traders, report section, portfolio parity | Review the seed portfolio's validation result before the holdout |
| 4. Campaigns | Campaign schema, generator, family quotas, registration, loop change, gateway tools, archive write-back, report | Review the first campaign queue, then re-enable research |

Research stays disabled (`research_enabled = false`) until phase 4. The paper fleet keeps running.

### 7.2 Migration

- Data unchanged: splits, segments and all trial records stay; validation and holdout remain unused.
- Mandates are immutable: the new statistics and portfolio rules go into new mandate versions on the same splits.
- `docs/contracts-v2.md` is not edited; the appendix carries the changes.
- The single-candidate flow stays in code for controls and benchmarks; the research loop no longer uses it.

### 7.3 Tests

- Templates: fixed-input values; no look-ahead (truncating later data leaves earlier positions unchanged);
  calendar-time parameter translation.
- Clustering and statistics: planted-cluster synthetic data; winsorisation; fixed-input values for `σ0`, `SR0(K)`,
  the split DSR and the appraisal-ratio test; the `max(σ0, σ_SR)` floor on a highly skewed series; flat trials;
  deterministic recomputation; idempotent backfill.
- Portfolio: blend within [0, 1]; sleeves, cap and probation weights; deadband (price drift alone never trades, full
  exits always do); bar-by-bar parity between backtest and paper.
- Gates: an uncorrelated modest variant passes; a strong redundant one fails; more than three registered variants
  are refused; a second registration in one campaign is refused.
- Probation: promotion after 90 days; demotion on the residual or parity condition.
- Campaigns: FakeAgent end-to-end (several sessions, registration, gates, conclusion, citation check, archive
  record); family quota exhaustion.
- Before each release: full suite and a local rehearsal (`zqr release --target local`).

### 7.4 Risks

- The registered test understates the search when registration follows exploration on the same data (6.3).
  Safeguards: prior requirement, do-no-harm validation, holdout, probation, split DSR reported.
- Portfolio construction can overfit: band, volatility target, cap and family definitions are fixed in phase 1;
  later changes count as trials.
- Validation and future holdouts are short; real out-of-sample evidence accrues in paper over time, tracked in the
  daily report.
- K depends on the threshold; the report shows all three.
