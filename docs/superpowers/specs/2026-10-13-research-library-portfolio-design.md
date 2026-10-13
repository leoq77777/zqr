# Design: strategy library, research memory, portfolio and campaigns

Status: approved in discussion on 2026-10-13, pending review of this document. Supersedes the desk's topic-rotation
research loop (`desk/research.py`) as the way research is directed. Contract changes in section 4.4 take effect only
after the human approves the contract appendix.

## 1. Problem

The desk ran 177 research sessions in 10 hours (2026-10-13, deepseek-flash, $6) and produced no candidate. The
sessions were independent: each started from scratch, rotated through four topics and wrote a fresh plugin, so
nothing accumulated. About 1,780 dev trials on two splits pushed the deflated-Sharpe threshold to an annual Sharpe
of about 7, which no strategy can reach.

Two causes:

1. **Research process.** Brute-force search without memory or direction. Every trial raises the bar for every
   later trial.
2. **Statistics.** DSR estimates the luck dispersion from the variance of Sharpe ratios across trials. That variance
   is dominated by trials that lose deterministically to costs (cluster Sharpe down to −11), not by luck.

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
| Architecture | Layered: strategy library, research memory (returns, clusters, family archives), portfolio layer, campaigns |

Universe stays BTC-USD and ETH-USD spot, long/cash. Live trading stays out of scope.

## 3. Strategy library and seeds

### 3.1 Concepts

| Concept | Content | Written by |
|---|---|---|
| Library entry | Template or frozen plugin with fixed parameters; source (publication, seed, campaign); family; rationale; adaptation notes; status `candidate` → `member` → `retired` | Seeds: human review and import. Campaign results: controller, after the portfolio gates |
| Family | Named research direction (e.g. medium-term trend) with a thesis and an archive (section 4.6) | Seed families: human. New families: proposed by clustering, confirmed by the human |
| Portfolio | Per product: members, blend rule, no-trade band, volatility overlay; immutable versions (section 5) | Controller only |
| Campaign | Goal, target family, budget, success criterion, conclusion (section 6) | Generator; the human can add or cancel |

Stored in SQLite and `ZQR_HOME`, managed by deterministic code. The agent reads them through gateway tools only.

### 3.2 Seed catalog

`library/seeds.yaml` plus a readable `docs/library/seeds.md` (sources, logic, parameter translation). Imported with
`zqr library import` after review. Only low-turnover families, because of costs:

| Family | Sources | Form here |
|---|---|---|
| Time-series momentum | Moskowitz, Ooi & Pedersen (2012); Hurst, Ooi & Pedersen, century of trend following (AQR); Liu & Tsyvinski (2021), 1–4 week crypto momentum | Long when the trailing 1/4/12-week return is positive; a continuous variant scales the position with signal strength |
| Moving-average trend | Brock, Lakonishok & LeBaron (1992); Baz et al. (2015), EWMA crossovers | Several fast/slow EWMA pairs, volatility-scaled to a 0–1 position |
| Channel breakout | Turtle rules (Faith, *Way of the Turtle*); Donchian channels | 20/55-day entry breakout, 10/20-day exit |
| Trend filter | Faber (2007), 10-month moving average | Long above the long moving average |
| Volatility management | Moreira & Muir (2017); Harvey et al. (2018) | Portfolio overlay: exposure scaled inversely to realised volatility, capped at 1 |

Liu & Tsyvinski's sample (2011–2018) does not overlap our data (from 2021), so it is an out-of-sample prior.
Short-term reversal is documented but cannot survive 0.6% per side; it is recorded in the archive as infeasible
under costs and not seeded.

### 3.3 Priors, not selection

- Canonical parameters from the sources, translated to hourly bars (20 days = 480 bars). No tuning. Two or three
  preregistered variants per family, about 15 entries.
- The seed portfolio includes every reviewed seed family, decided before any data is looked at.
- Seeds run once on dev to characterise families (returns, turnover, correlations) and to seed the clustering, not
  to select.

### 3.4 New templates

Time-series momentum (binary and continuous), EWMA crossover, breakout with an exit channel, long moving-average
filter, volatility overlay. Existing `sma_cross`, `momentum` and `breakout` are reused where they match.

## 4. Research memory

### 4.1 Return archive

Every completed dev experiment's equity curve is reduced to daily returns (about 1,270 points over 3.5 years) and
stored per split (`ZQR_HOME/library/returns/<split_id>.parquet`, one column per experiment) with an index table
(experiment, split, turnover, Sharpe). The 1,780 existing trials are backfilled; their equity curves exist.

### 4.2 Removing the market component

Long/cash strategies all hold the coin part of the time, so their returns are positively correlated through the
coin. Clustering raw returns put 875 of 879 BTC trials in one cluster (effective N = 3).

Each trial's daily returns are regressed on the product's buy-and-hold daily returns over the same window; the
residual is clustered. Trials that never trade (zero variance) form one "flat" cluster, counted once.

### 4.3 Clustering

- Distance `sqrt((1 − ρ) / 2)` on residual correlations; average-linkage hierarchical clustering.
- Cut at fixed residual-correlation thresholds 0.3, 0.5 and 0.7. Gates use the largest K of the three (most
  conservative). Silhouette-based selection is not used (it chose k near the search limit in the feasibility run).
- Deterministic; recomputed in full after each batch of trials (seconds for 2,000 trials).
- Adds the `scipy` dependency (about 40 MB in the image).

Feasibility run on 2026-10-13 data (market component removed via the first principal component, about 50% of
variance): about 147 clusters on BTC and 145 on ETH.

### 4.4 Effective trials and the new DSR (contract appendix)

- K = number of clusters over every trial ever run on the split (old trials, seeds, failures). Never reset.
- For a candidate daily return series with annualised Sharpe `SR`, length `T`, skewness `γ3`, kurtosis `γ4`:
  - `σ_SR = sqrt((1 − γ3·SR + (γ4 − 1)/4·SR²) / (T − 1))` (per period, annualised for reporting);
  - `SR0 = σ_SR · ((1 − γ)·Φ⁻¹(1 − 1/K) + γ·Φ⁻¹(1 − 1/(K·e)))`, γ the Euler–Mascheroni constant: the expected
    maximum Sharpe of K zero-skill strategies;
  - `DSR = Φ((SR − SR0) / σ_SR)`; acceptance requires DSR ≥ 0.9 (unchanged).
- Why: DSR asks how good the best of K useless strategies looks by luck. Under that null every strategy's Sharpe
  estimate has dispersion `σ_SR`. The cross-trial variance used today mixes in deterministic cost losses.

With 2026-10-13 data:

| | BTC-USD | ETH-USD |
|---|---|---|
| K | ~148 | ~146 |
| σ_SR (annual) | ~0.32 | ~0.51 (fatter tails) |
| SR0 | ~0.84 | ~1.34 |
| Sharpe needed for DSR 0.9 | ~1.24 | ~1.99 |
| Current best dev trial | 1.13 → DSR 0.82 | 1.36 → DSR 0.51 |
| Current rule (for comparison) | needs ~7.7 | needs ~7.1 |

The new rule is more lenient than the current one; safeguards are the conservative K, the holdout, and paper
trading. This changes the acceptance definition in `docs/contracts-v2.md`; it is written as a contract appendix
(`docs/contracts-v2-appendix-a.md`) and takes effect only after human approval, through new mandate versions.

### 4.5 Clusters and families

- Library entries and campaign trials carry a family tag. Old trials are tagged from their session topic and
  cluster.
- Each cluster maps to the family holding the majority of its tagged members. A cluster matching no family produces
  a new-family proposal (member parameters, code summary, statistics); the human confirms it.
- Cluster ids are internal and may change between recomputations; families are the stable, human-facing unit.

### 4.6 Family archives

One per family.

Deterministic part, regenerated after each clustering:

- members and the representative per product, with dev statistics;
- parameter ranges tried, trials and clusters used;
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

Per product: members (library entries), blend rule, no-trade band, volatility overlay switch. Immutable, hashed,
versioned; lifecycle draft → validated → frozen → holdout → paper, like candidates today.

### 5.2 Blending

Each member outputs a target position in [0, 1] per bar.

1. Within a family: equal-weight average of members.
2. Across families: equal capital sleeves (with F families each uses at most 1/F of capital). Inverse-volatility
   weights would favour families that are mostly in cash, since all trade the same coin.
3. Volatility overlay (a library entry, subject to the same gates): final = blend × min(1, target vol / realised
   vol).

The same blending code serves backtests and paper trading.

### 5.3 Turnover

Blending smooths switching. The existing no-trade band applies to the blended target (rebalance only when the
target differs from the current position by more than a fixed fraction, e.g. 10%). Band width, volatility target
and family sleeves are fixed when the seed portfolio is defined; changing them later counts as a trial.

### 5.4 Admission gates for new members

| Gate | Segment | Test | Standard |
|---|---|---|---|
| Selection | dev | Return difference between the portfolio with and without the candidate; DSR of that series with the split's K | DSR ≥ 0.9 |
| Confirmation | validation (never shown to the agent) | Portfolio with the candidate vs without: Sharpe, drawdown, turnover | Fixed in advance, e.g. Sharpe improvement > 0, drawdown no more than 2 points worse |
| Final | holdout | New portfolio version as a whole | Current rule: Sharpe ≥ 0.5 and return > 0 |

A candidate is judged by what it adds to the portfolio. A modest but uncorrelated strategy can be accepted; a
strong but redundant one can be rejected. Validation is about 1.2 years, too short for a significance test, so it
confirms only.

### 5.5 Holdout budget

Each split has 10 holdout viewings; each new portfolio version uses one. Accepted changes are batched into versions
(several accepted changes, or at least a week apart).

### 5.6 Retirement

Periodic reverse test: remove a member; if the portfolio improves on dev and validation, the system proposes
retirement. In this phase the human confirms. Retired entries stay in the library with the reason in the archive.

### 5.7 Seed portfolio

All reviewed seed families, equal sleeves; members, weights and band fixed before validation or holdout is
looked at. Validation standard fixed in advance: Sharpe ≥ 0.5 and max drawdown not worse than −25%. The result is
shown to the human before the holdout is used. If it fails validation, it is recorded and discussed; it is not
adjusted and retried automatically.

### 5.8 Paper and report

Paper adds one portfolio trader per product; the 16 single strategies stay as controls. The daily report adds:

- portfolio return against buy and hold;
- contribution by family;
- current composition, turnover and costs;
- decision parity, bar by bar, as today.

Portfolio diagnostics (drawdowns by regime, concentration, weak periods) feed campaign generation.

## 6. Campaigns

### 6.1 Campaign record

| Field | Example |
|---|---|
| Goal | Reduce medium-term trend drawdowns in range markets without hurting trend markets |
| Target family, product | Medium-term trend; BTC-USD |
| Success criterion (preregistered) | A candidate passes the selection gate (5.4) |
| Budget | 30 experiments, 4 sessions, $2 |
| Status | queued → running → concluded / cancelled |
| Conclusion | Hypothesis, evidence, verdict (supported / refuted / inconclusive), candidates, open questions |

### 6.2 Generation

Deterministic, from portfolio diagnostics and archives:

1. regime gaps (drawdowns concentrated in one regime);
2. concentration (one family dominates return or risk) → look for uncorrelated families;
3. cost (a family unprofitable at 2× cost) → lower-turnover variants;
4. open questions from earlier conclusions → follow-up campaigns;
5. exploration (unclaimed clusters, or no new family for a while), a fixed share (about 20%) of campaigns.

Ranked by gap size × room left in the family. The human can list, cancel or add campaigns.

### 6.3 Execution

- A campaign is a sequence of sessions with memory. Each session's brief has the goal and criterion, the target
  family archive, summaries of other families, notes from earlier sessions of the campaign, and the remaining
  budget.
- Each session writes progress notes in `research.json`.
- The campaign ends when the budget is spent, the criterion is met, or the agent concludes. The last session writes
  the conclusion; citations are checked; the record is appended to the family archive.
- Candidates go through the gates in 5.4, run by deterministic code.

### 6.4 Gateway tools (dev only)

- family archive query;
- current portfolio members and their dev statistics;
- marginal evaluation on dev (portfolio with the candidate); counts as a trial.

### 6.5 Pace and cost

- One campaign at a time; idle (no model calls) when the queue is empty.
- Generation after each campaign, after each portfolio version, and daily.
- Daily cap ($20) stays; each campaign has its own cap.
- Research capacity per split: when K reaches a level where the threshold is unrealistic (e.g. K > 300), no more
  campaigns are generated on that split; the desk reports that new data must accumulate before a new split.

### 6.6 Replacing the topic loop

Topic rotation is removed; the research loop takes the next session from the campaign queue. Gateway, isolation,
audit, cost ledger, standing authorization and recovery stay. The report's research section shows campaigns,
conclusions, archive changes and budget. Each campaign can name its model, for model comparisons on like tasks.

## 7. Delivery

### 7.1 Phases

| Phase | Scope | Human checkpoint |
|---|---|---|
| 1. Library and seeds | Templates, library and family schema, seed catalog, `zqr library list/show/import` | Review `docs/library/seeds.md`, then import |
| 2. Memory, clustering, DSR | Return archive and backfill, residual clustering, K at three thresholds, new DSR, deterministic archives, import of 177 session conclusions | Approve the contract appendix; review the clustering report on old trials |
| 3. Portfolio | Portfolio versions, blending engine, gates, seed portfolio, paper portfolio traders, report section, portfolio parity | Review the seed portfolio's validation result before the holdout |
| 4. Campaigns | Campaign schema, generator, loop change, gateway tools, archive write-back, report | Review the first campaign queue, then re-enable research |

Research stays disabled (`research_enabled = false`) until phase 4. The paper fleet keeps running.

### 7.2 Migration

- Data unchanged: splits, segments and all trial records stay; validation and holdout remain unused.
- Mandates are immutable: the new DSR and portfolio rules go into new mandate versions on the same splits.
- `docs/contracts-v2.md` is not edited; the appendix carries the changes.
- The single-candidate flow stays in code for controls and benchmarks; the research loop no longer uses it.

### 7.3 Tests

- Templates: fixed-input values; no look-ahead (truncating later data leaves earlier positions unchanged);
  parameter translation.
- Clustering and DSR: planted-cluster synthetic data; fixed-input DSR values; flat trials; deterministic
  recomputation; idempotent backfill.
- Portfolio: blend within [0, 1], sleeves, band; bar-by-bar parity between backtest and paper.
- Gates: an uncorrelated modest candidate is accepted; a strong redundant one is rejected.
- Campaigns: FakeAgent end-to-end (several sessions, budget exhausted, conclusion, citation check, archive record).
- Before each release: full suite and a local rehearsal (`zqr release --target local`).

### 7.4 Risks

- Portfolio construction can overfit: band, volatility target and family definitions are fixed in phase 1; later
  changes count as trials.
- Validation is short (about 1.2 years); real out-of-sample evidence comes from the holdout and from paper over
  time, tracked in the daily report.
- K estimation depends on the threshold; gates use the most conservative K and the report shows all three.
