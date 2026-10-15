# Phase 4: campaigns and admission gates — plan (draft for review)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** D1-D8 approved by the human on 2026-10-15. Tasks 0-2 (holdout closure, shadow portfolios) shipped as
release 4a (`0.1.0-d74225f9`, 2026-10-15). E1 (longer dev) approved with F1-F4 and amendment A13.4; its code
(E1.1-E1.7) is built for release 4b (end of this plan). Tasks 3-16 are an outline until the long dev exists.

**Goal:** Replace the topic loop with campaigns that produce registered hypotheses, run admission gates 1-2 on
them, and build up a queue of portfolio versions for the next split's holdout. Keep the seed portfolios running as
shadow paper traders so forward evidence accrues. Stop at the design's checkpoint: the human reviews the first
campaign queue, then re-enables research.

**Rules:** design sections 4.6, 5.4-5.7, 6, 7.1; appendix A6 (registration, gates 1-2), A9, A10, A11;
investment policy sections 2 and 5; seed review `docs/reviews/2026-10-15-seed-portfolio-review.md`.

**Branch:** `phase4` from `main` (`ed285a5` or later).

---

## Where phase 3 left us

- No portfolio version is in paper: BTC `pv-4864fc813a30` is `failed_final`, ETH `pv-6b52caaf11b5` is
  `failed_validation`. Both are terminal.
- Holdout viewings: BTC 1 of 10, ETH 0 of 10. Validation uses: 1 per split. Next split from 2027-10-12 (A11).
- The phase 3b outline assumed a seed portfolio in paper: version switching "on the same paper account", probation
  in paper and gate 1 against "the current paper version". None of that has a base now.
- Research is disabled (`research_enabled = false`); the paper fleet runs 16 single-strategy controls.

## Decisions for the human (approved 2026-10-15)

- **D1. Close the current splits' holdouts to new versions.** The human has seen the BTC holdout path quarter by
  quarter, and the BTC and ETH holdouts cover the same dates. Any new version on either current split would be
  built with that knowledge, so a holdout viewing would test a hypothesis on the data that suggested it.
  - Proposal: an amendment A13.3 closes `split-1016c7c9873a` and `split-c47a0be57a24` to all holdout runs
    (portfolio versions and single candidates). A closed split refuses every holdout request; a repeat of an
    earlier viewing still returns the recorded result. Gate 3 resumes on the next split.
  - The closure is a recorded row (`holdout_closures`: split, reason, human note, time), shown in the report. It is
    not "contaminated": no unrecorded viewing happened.
  - Alternative: keep A9 as it is (9 BTC and 10 ETH viewings left, one version per split per 7 days), relying on
    registration and gates 1-2. Not recommended: the human registers hypotheses in this phase and has seen the
    holdout.
- **D2. Shadow paper for both seed portfolios.** Run `pv-4864fc813a30` and `pv-6b52caaf11b5` as paper portfolio
  traders marked shadow, from release onwards.
  - Why: the only evidence that cannot be snooped accrues forward. The phase 3a paper path (portfolio trader,
    ladder, resume, portfolio parity) also gets its first production run without capital being "approved".
  - Rules: a new command `zqr portfolio shadow <vid> --note` (human) accepts `failed_validation`, `failed_final`
    and `validated`; the version's state does not change (the shadow start is an event). Roster name
    `shadow-<product>`. The report shows shadows in their own table, labelled "not approved", with parity, ladder
    and buy and hold; they are excluded from the combined account and from the soft stop. Library entries keep
    their status (no move to member).
  - Consequence: the human then watches the seed portfolios on data that will fall into the next split's
    validation and holdout. For the seed versions, the forward paper record replaces that holdout as evidence; for
    later versions the next holdout stays unseen except through what shadows and controls show.
  - Alternative: no shadows; the seed versions stay archived.
- **D3. Merge phase 3b into phase 4 and defer what needs a paper portfolio.**
  - In this phase: registration (A6), gate 1 (marginal value on dev), gate 2 (do no harm on validation), campaigns
    (design 6), and admitted versions waiting for the next split.
  - Deferred until a version reaches paper: version switching on a paper account, probation (A10), retirement
    proposals (design 5.7).
- **D4. Gate 1 reference.** `R_p` is the seed portfolio of the product at full weights on dev (A6 says "the seed
  portfolio until another version is frozen"; the frozen BTC version is the seed itself). Unchanged when a
  candidate passes gate 2: each candidate is measured against the seed, not against other candidates, until a new
  version is frozen on the next split.
- **D5. What a passing candidate produces.** A candidate that passes gates 1 and 2 yields a portfolio version
  "seed + member" in a new state `admitted`, which holds until the next split is cut. On the new split the version
  is redefined (new split id, new dev) and runs gates 1-2 and the seed standard again before any holdout; several
  admitted members may be batched into one version then (A9). The agent sees only the verdicts (A6).
- **D6. Campaign diagnostics come from dev only.** Generation (design 6.2: regime gaps, concentration, cost, open
  questions, exploration about 20%) reads the seed portfolio's dev run and the family archives. Validation,
  holdout and shadow paper metrics never feed generation or session briefs. Human-added campaigns must cite a
  published prior or dev archive evidence.
- **D7. Research budget rules (investment policy 5, "to define in phase 4").**
  - Reservation: a session reserves its maximum cost against the campaign, monthly ($83.33) and daily caps before
    it starts and is refused if the reservation does not fit. Unpriced model calls stop the loop until a price is
    configured (today they count as $0, `desk/research.py` `_cost`).
  - Campaign: $1 reserved at the start. One top-up of $2 when a registered variant passes gate 1; a second of $2
    when it passes gate 2; never more than $5. No top-up on anything else.
  - Daily cap: lowered from $20 to $5, so one bad day cannot burn two months of budget.
- **D8. Single-candidate promotion leaves the research loop.** Sessions no longer freeze or promote single
  candidates (design 6.6, 7.3). The single-candidate flow stays in code for controls and benchmarks; the standing
  authorization no longer covers research output.

**Also in this phase (engineering debts found in phases 2-3):**

- Topic tagging clears existing tags when `desk/research_ledger.jsonl` is missing (seen in the phase 3
  rehearsal): refuse to tag without the ledger.
- The rejected-proposals section of the clustering report lacks cluster details.

## Task outline

0. [x] **Record decisions.** Amendment A13.3 (holdout closure) in the appendix; investment policy section 5 (D7
   budget rules; implemented in Task 9, research stays disabled until then); `docs/development.md` sections 4, 5.9,
   6 and the commands table.
1. [x] **Holdout closure.** `holdout_closures` table (`ExperimentStore.close_holdout` / `holdout_closure`);
   `closed_refusal` checked in `run_holdout` and `run_portfolio_holdout` before the cap (repeats still answer, an
   override is a recorded contamination); promotion stops before freezing; `zqr close-holdout --split --note`;
   report column "(closed)".
2. [x] **Shadow portfolios.** `RosterEntry.shadow` (not in the identity hash); `zqr portfolio shadow <vid> --note`
   for `validated`, `failed_validation`, `failed_final`, roster name `shadow-<product>`, version event without a
   state change, library untouched; daily report table "Shadow portfolios (not approved)" with parity; monthly
   review rows labelled; combined account and soft stop read approved books only.

   **Release 4a and after it (human commands on the host):**
   `zqr close-holdout --split split-1016c7c9873a --note ...`, `zqr close-holdout --split split-c47a0be57a24
   --note ...`, `zqr portfolio shadow pv-4864fc813a30 --note ...`, `zqr portfolio shadow pv-6b52caaf11b5
   --note ...`; the shadows start at the next fleet roster reload (cash, from the release day).
3. **Hypothesis registration.** Immutable `hypotheses` table: prior text, citations (published or experiment
   ids), 1-3 variants (code hash, parameters), campaign id. More than three variants or a second registration per
   campaign refused. Human CLI `zqr hypothesis register` and a gateway tool for campaigns.
4. **Gate 1, marginal value.** A6 regression of each variant's dev returns on the seed portfolio's; appraisal
   ratio, `Φ((AR − SR0(K))/σ_h) ≥ 0.9`; each evaluation is a trial; best passing variant goes on.
5. **Gate 2, do no harm.** Portfolio with the variant at full weight against the seed portfolio on validation:
   Sharpe not lower, drawdown at most 2 points worse, turnover at most 25% higher. Counted in `validation_uses`;
   verdict-only output for agents.
6. **Admitted versions.** State `admitted`; `zqr portfolio list --state admitted`; the report lists them with the
   next split date.
7. **Campaign record and store.** Fields of design 6.1; queued → running → concluded / cancelled; human CLI
   `zqr campaign list/show/add/cancel`.
8. **Generator and family quotas.** Deterministic generation from dev diagnostics and archives (D6); ranking by
   gap size × room left; per-family trial quota per split; exploration share.
9. **Budget reservation.** D7 in `desk/research.py` and the research ledger; unpriced stops the loop.
10. **Loop change.** The research loop takes the next session from the campaign queue; session brief with goal,
    family archive (agent view), other families' summaries, earlier session notes and remaining budget; progress
    notes in `research.json`; no single-candidate promotion (D8).
11. **Gateway tools.** Family archive query (agent view), seed portfolio members with dev statistics, marginal
    evaluation on dev (a trial), hypothesis registration.
12. **Conclusions and archive write-back.** The last session writes the conclusion; citations checked; record
    appended to the family archive.
13. **Report.** Research section: campaigns, registrations and gate verdicts, admitted versions, budget used
    against the monthly cap, cost per conclusion.
14. **Debts.** Tagging guard; rejected-proposal details.
15. **Tests, docs, rehearsal.** FakeAgent end to end (several sessions, registration, gates, conclusion,
    citation check, archive record); quota exhaustion; closed-holdout refusal; shadow trader replay. Full suite and
    a local rehearsal on a copy of production data.
16. **Release and checkpoint.** Ask before merging and releasing. After release: `zqr validation close-holdout`
    and `zqr portfolio shadow` by the human; generate the first campaign queue; the human reviews it, then sets
    `research_enabled = true`.

## Data decisions (open, raised by the human on 2026-10-15)

The research data starts on 2021-01-01, a start date chosen when the project began fetching, not a limit of the
source. Checked on 2026-10-15 from the production host (probe in a temporary directory, production data unchanged):

| | Coinbase hourly from | Extra history before 2021 | Coverage 2015/16-2020 | Missing bars |
|---|---|---|---|---|
| BTC-USD | 2015-07-20 21:00 | 5.5 years (47,745 bars) | 99.87% | 42 in 18 gaps (largest 15 h, 2018-08-10) |
| ETH-USD | 2016-05-18 00:00 | 4.6 years (40,336 bars) | 99.51% | 200 in 61 gaps, 175 of them in the first week after listing |

The extra years hold the 2017 bubble, the 2018 bear market (BTC −84%), the 2019 recovery and the March 2020 crash:
the regimes the seed portfolios have not been tested in. They cannot add unseen validation or holdout data (those
must lie after 2026-10-13), but they add dev: about 3.5 → 8.9 years for BTC and 3.5 → 8.1 for ETH, which lowers the
standard error of an annualised Sharpe from about 0.54 to 0.34 and the gate 1 appraisal-ratio bar for one variant
from about 0.69 to about 0.43.

Options considered (E1 approved by the human on 2026-10-15; E2-E4 not taken now):

- **E1. Extend dev backwards (approved).** Fetch from the listing dates; freeze new datasets (the current ones stay);
  create new splits per product with dev from the listing date to 2024-06-20 and the same validation and holdout
  boundaries, closed at creation (A13.3, the same data); new desk mandates written by the human. An amendment says
  how the old splits' trials count on the new ones (draft A13.4 below). Seed characterisation runs (A7) and the
  seed portfolios' dev runs are repeated on the new dev; the 2015/16-2020 part is reported separately as the first
  look at those years. Gate 1 and campaign diagnostics then run on the long dev. Detailed as Tasks E1.1-E1.8 below.
- **E2. Keep the early years as a one-time backward test** of the frozen seed versions before anyone looks at them.
  Weak as out-of-sample evidence (the seeds' published priors were estimated on 2014-2020 crypto data, and market
  history is common knowledge); E1 includes the same first look as a diagnostic.
- **E3. Other coins as research-only evidence** (more breadth: does a rule work beyond BTC and ETH). Few Coinbase
  coins have long history (LTC 2016, BCH 2017; most others 2021 or later) and they correlate strongly with BTC.
  Needs a mandate and K design across instruments; a later phase.
- **E4. Longer daily history from another venue** (Bitstamp BTC from 2011). Different venue and prices, daily
  families only, a new data client and contract; a later phase.

Data alone does not fix the holdout: periods already seen stay seen. Unseen evidence accrues only forward (shadows,
controls, the next split from 2027-10-12).

## E1: longer dev (approved 2026-10-15, with F1-F4 and A13.4)

**Status:** F1-F4 and the draft A13.4 approved by the human on 2026-10-15. A13.4 is in the appendix, with one
check added while building it: the child's dataset must extend the parent's (the parent's bars copied verbatim).
E1.1-E1.4, E1.6 and E1.7 built (300 tests). The mandate switch is refused while research is enabled (simpler and
stricter than detecting a running session).

**What changes and what does not.**

- New per product: a dataset that extends the current one backwards (the current bars from 2021-01-01 are copied
  verbatim, not re-fetched), a split with the same validation and holdout windows, a desk mandate pointing at it.
- The current datasets, splits, mandates, trials, clusterings and versions stay as they are (immutable records).
  The seed versions keep running as shadows; a paper trader reads live bars and does not depend on a split.
- Gate 1's bar depends only on the number of registered variants and on T (A6). On the long dev it falls from
  0.688 to 0.429 (BTC) and 0.451 (ETH) for one variant. Inheriting K (A13.4) changes the split DSR (A5, reported,
  not a gate) and the legacy per-experiment DSR of the single-candidate flow (controls and benchmarks); it does not
  change gate 1.
- Validation stays the data already used once per split (the seed validation runs). Gate 2 on the new split
  re-uses it; the report shows the parent's validation uses next to the new split's (A13.4).

**Reference values on the long dev** (dev end 2024-06-20 18:00, last day partial, A1 day grid; "needed" = annual
Sharpe giving 0.9 at `σ = σ0`):

| Split | Dev from | T | `σ0` | Needed K=1 | K=2 | K=3 | K=20 | K=250 |
|---|---|---|---|---|---|---|---|---|
| current (both) | 2021-01-01 | 1,267 | 0.5369 | 0.688 | 0.967 | 1.146 | 1.709 | 2.212 |
| new BTC (F1) | 2015-07-21 | 3,258 | 0.3348 | 0.429 | 0.603 | 0.715 | 1.065 | 1.379 |
| new ETH (F1) | 2016-06-01 | 2,942 | 0.3523 | 0.451 | 0.635 | 0.752 | 1.121 | 1.451 |

K = 250 is about the inherited K(0.7) (231 BTC, 240 ETH on 2026-10-13) plus the new split's own seed trials.

### Sub-decisions (approved 2026-10-15 as proposed)

- **F1. Start dates.** BTC from 2015-07-21 00:00 UTC (the first full UTC day; the first bar is 2015-07-20 21:00).
  ETH from 2016-06-01 00:00 UTC, skipping the listing fortnight that holds 175 of the 200 missing ETH bars (the
  first two weeks trade intermittently; flat-filling them would invent prices). Alternative: from the first bar.
- **F2. Trials of the parent split count on the child (draft A13.4 below).** Proposal: the parent's K(0.7) and
  trial count are added in full. Alternatives: no inheritance (K starts from the new seed runs only: the search on
  2021-2024 shaped every later idea, so this understates it); a share proportional to the overlap (2021-2024 is
  39% of the BTC long dev and 43% of ETH; looks precise without a basis).
- **F3. Desk mandates.** The human writes them: a copy of each current mandate with `dataset_id`, `split_id` and
  `notes` changed, thresholds unchanged (the single-candidate flow they govern serves controls and benchmarks
  only, D8). The switch of the desk's mandate per product is a recorded human command (Task E1.4), not a config
  edit.
- **F4. Seed portfolio on the new split: dev only.** The seed version is redefined on the new split (a new version
  id, the same members) and run on dev only, as gate 1's reference `R_p` (D4); a dev run is a trial (A14.2). No
  validation run: the validation data is the same one the seed versions were already judged on (A6: a failure is
  recorded, not retried). The version stays `draft` with a recorded event "gate 1 reference".

### Draft amendment A13.4 (approved; the appendix holds the final text)

> **A13.4 Splits that extend an earlier split's dev backwards (draft 2026-10-15)**
>
> **Problem.** A split owns its trials, K and holdout viewings (A11). E1 creates splits whose dev contains the
> whole dev of an earlier split (2021-01-01 to 2024-06-20) and whose validation and holdout are the same windows.
> Every idea tested on the earlier split was shaped by that data; counting trials from zero on the new split would
> report a search that did not happen.
>
> **Amended rule (adds to A3, A9 and A11).**
>
> - A split may name a parent: `zqr split-lineage --split <child> --parent <parent> --note <reason>` (human),
>   recorded once (`split_lineage`: child, parent, note, time). Allowed only when both splits are for the same
>   product and granularity, the child's validation and holdout windows equal the parent's, and the child's dev
>   contains the parent's dev.
> - The child's K at each threshold is its own clustering's K plus the parent's (the parent's latest clustering,
>   recursively); `K(0.7)` so obtained enters every calculation on the child (A4, A5). The child's trial count for
>   the legacy DSR (contracts v2 section 6) is likewise its own plus the parent's.
> - The child inherits the parent's holdout closure (A13.3): a child of a closed split is closed when the lineage
>   is recorded, with a note naming the parent's closure. Holdout viewings and validation uses stay per split; the
>   report shows the parent's next to the child's.
> - Reports show K as "own + inherited (parent)". Clustering runs per split as before; the parent's trials are not
>   re-run or re-clustered on the child.
> - Seed characterisation runs (A7) and seed portfolio dev runs are repeated on the child. Both splits' seed trials
>   then count on the child (at most 12 + 1 per product twice); conservative, accepted for simplicity.
>
> **Applied:** the E1 splits of BTC-USD and ETH-USD, parents `split-1016c7c9873a` and `split-c47a0be57a24`.
>
> **Implementation.** `split_lineage` table; `memory/clusters.py` `k_breakdown` / `effective_k`;
> `split_trial_count` used by the legacy DSR (`experiments/runner.py`, `validation/walkforward.py`) and reports;
> lineage command closes the child. Tests: K and trial count add the parent's, recursively; lineage refused on
> different windows or a dev that does not contain the parent's; a child of a closed split refuses a holdout.

### Tasks

Order: E1.1-E1.4 and E1.6-E1.7 are code (one release, "4b"); E1.5 and the host steps run after it. Tasks 3-16
follow on the long dev.

**E1.1 Dataset extension.** Files: `src/quant_research/data/snapshot.py`, `src/quant_research/cli.py`,
`tests/test_data.py`.

- [x] Test: a synthetic base dataset and a prefix ending exactly one bar before the base's first bar give an
  extended dataset whose rows from the base's first bar are bit-identical to the base (content hash of that slice
  equals the base `data_hash`), whose manifest has `extends`, `base_data_hash`, `requested_start` = prefix start,
  combined coverage and gaps (prefix gaps + base gaps), and which is idempotent.
- [x] Test: refused when the prefix leaves a gap at the junction, overlaps the base, or differs in venue,
  instrument, granularity or fill policy; refused for a synthetic base.
- [x] `extend_dataset(paths, base_id, prefix_bars, prefix_report, raw_dir)`: checks as above, `pd.concat`, combined
  `ValidationReport`, `freeze_dataset(..., extra={"extends": ..., "base_data_hash": ..., "junction": {...}})` where
  `junction` records the base's first open against the prefix's last close (relative jump, informational).
- [x] CLI `zqr extend-dataset --dataset <base> --start <iso> [--fill-policy ffill_flat]`: validates raw candles of
  the base's product in `[start, base first open)` (the existing raw directory; `zqr fetch-data` already writes
  window files that do not collide with the 2021 windows) and freezes. Output: new id, rows, coverage, gaps,
  junction.
- [x] `zqr datasets` shows `extends`.

**E1.2 Split lineage.** Files: `src/quant_research/validation/splits.py`, `src/quant_research/experiments/store.py`,
`src/quant_research/cli.py`, tests in `tests/test_experiments.py`.

- [x] `zqr create-split --dataset <id> --like <split>`: takes the other split's dev end and validation end; refuses
  unless the resulting holdout window equals the other split's.
- [x] Table `split_lineage (split_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, note TEXT NOT NULL, at REAL NOT
  NULL)`; `ExperimentStore.record_lineage` / `split_parent`; checks of A13.4 (same product and granularity, equal
  validation and holdout windows, child dev contains parent dev); recording twice keeps the first.
- [x] Recording a lineage under a closed parent closes the child (`close_holdout`, note "inherits the closure of
  <parent>: <parent note>").
- [x] CLI `zqr split-lineage --split --parent --note` (human): note required; output lineage, closure, both splits'
  windows.
- [x] Tests: refused cases; closure inherited; a holdout request on the child is refused without loading holdout
  data.

**E1.3 K and trial inheritance.** Files: `src/quant_research/memory/clusters.py`, `src/quant_research/experiments/store.py`,
`src/quant_research/experiments/runner.py`, `src/quant_research/validation/walkforward.py`,
`src/quant_research/memory/characterise.py`, `src/quant_research/desk/report.py`, `src/quant_research/memory/commands.py`,
tests in `tests/test_memory.py`.

- [x] `k_breakdown(store, split_id) -> {"own": {...}, "inherited": {...}, "parent": id | None}` at the three
  thresholds (recursive; a parent with no clustering contributes 0 and is flagged); `effective_k` returns own +
  inherited at 0.7.
- [x] `split_trial_count(store, split_id)` = own + parent's (recursive); used for the legacy DSR in
  `run_experiment` and walk-forward and in the reports' trial columns. `ExperimentStore.trial_count` itself stays
  per split (the return archive and A1 depend on it).
- [x] `zqr memory status` and the daily report show "K(0.7) own + inherited (parent)"; the report's holdout and
  validation columns show the parent's counts next to the child's.
- [x] Tests: a child with its own clustering and a parent's gets the sum at each threshold; a grandchild sums
  both; the legacy DSR of a child experiment deflates with the summed count.

**E1.4 Desk mandate switch.** Files: `src/quant_research/desk/config.py`, `src/quant_research/desk/commands.py`, tests
in `tests/test_desk.py`.

- [x] `zqr desk use-mandate --product --mandate --note` (human): the mandate must be stored, for the product (its
  dataset's instrument), with a split; writes `cfg.mandates[product]` and appends `{product, from, to, note, at}`
  to `desk/mandate_history.jsonl`. Refused while a research session is running.
- [x] Report: the trials table names the mandate's split and its parent.
- [x] Test: switch recorded, refused for a mandate of another product, `seed_version` and `characterise` follow
  the new mandate.

**E1.5 Seed characterisation and archive on the new splits (host, after release).** No code beyond E1.3: `zqr
memory characterise` runs the 12 instances per product on the desk mandates' splits (now the new ones); `zqr memory
run --sandbox docker` archives, clusters and regenerates the family archives (both splits listed; the new one
labelled with its parent).

- [ ] Check: T = 3,258 (BTC) and 2,942 (ETH) daily returns per new seed trial (F1 dates); K shown own + inherited.

**E1.6 Seed portfolio dev reference.** Files: `src/quant_research/portfolio/evaluate.py`,
`src/quant_research/portfolio/commands.py`, tests in `tests/test_portfolio.py`.

- [x] `zqr portfolio dev <vid> [--reference]`: `run_segment(..., "dev")` only (a trial, archived; no validation
  use); the state stays `draft`; `--reference` records the event `{"gate1_reference": true}` (human or controller;
  one reference per product and split, a second refused).
- [x] Test: dev run archived and counted, no validation use, state unchanged, second reference refused.

**E1.7 First look at the early years.** Files: `src/quant_research/portfolio/evaluate.py` (period statistics),
`src/quant_research/memory/characterise.py`, `src/quant_research/portfolio/commands.py`, tests.

- [x] `period_stats(equity, cuts)`: A4 Sharpe, total return, maximum drawdown, exposure and buy and hold per
  period, from a recorded dev run's equity (no re-run).
- [x] `zqr portfolio periods <vid> --cut 2021-01-01` and `zqr library periods --product <p> --cut 2021-01-01`
  (each seed instance's latest seed run on the desk split). Periods: listing-2020, 2021-dev end, whole dev. The
  ladder's actions and the yellow time per period for the portfolio.
- [x] These are dev statistics: allowed in campaign diagnostics and agent briefs (D6).

**E1.8 Docs, tests, rehearsal, release 4b.**

- [x] A13.4 into the appendix as approved (or as decided); `docs/development.md` commands table, invariants
  (lineage, inheritance), data section (extended datasets).
- [x] Full suite and ruff (301 passed). Rehearsal on a copy of production data (fetch into the copy, host steps 1-8
  below), the production data unchanged.
- [x] Rehearsal finding, fixed: a parent closed after the lineage did not close the child; closure is now read
  through the ancestors (`closed_refusal`), as the A13.4 text says.
- [x] Rehearsal finding, decided (A13.5 approved): bad prints in the pre-2021 candles. BTC 2017-04-15 23:00 has open and low 0.06
  (neighbours near 1,180); seeds that entered at that open show returns of 20,000x and more. ETH 2017-06-21 19:00
  has low 0.10 (the GDAX flash crash; Turtle's exit channel reads it). A rule "open or low below half of
  min(previous close, close), or open or high above twice the max" flags exactly these two bars in nine years and
  none in the 2021+ data. A third (ETH 2017-04-15 23:00, open 74.98 against closes near 48.5) escapes that rule;
  the isolated-print rule of A13.5 (drafted, awaiting approval) catches all three and nothing else.
- [x] Rehearsal finding, decided (A13.6 approved, gate 2 included): the ladder liquidates and never resumes in a
  backtest. BTC seed portfolio liquidated
  2020-03-12 (48% of dev in cash), ETH 2017-09-15 (84%). As gate 1's reference this is mostly zero. Without the
  ladder: BTC Sharpe 1.89 (2015-2020) and 0.83 (2021-2024), drawdown -39%; ETH 1.54 and 1.11, drawdown -39% (still
  with the bad prints). The human chose the run without the ladder (2026-10-15); A13.6 drafted, awaiting approval.
- [x] A13.5 and A13.6 implemented (303 tests). Second rehearsal on a fresh production copy (with the 4a closures):
  extension drops exactly the three bars; the lineage records the inherited closures; K(0.7) BTC 10 + 231 = 241,
  ETH 10 + 240 = 250; the archive holds the portfolio dev runs without the ladder. Seed portfolios on the long dev,
  without the ladder: BTC Sharpe 1.89 (2015-2020) / 0.83 (2021-2024) / 1.50 whole, drawdown −39%; ETH 1.64 / 1.11
  / 1.41, drawdown −33%. With the ladder BTC liquidates 2020-03-12, ETH 2023-09-11 (the 2017 ETH liquidation of the
  first rehearsal came from the bad prints).
- [x] Ask before merging and releasing. Released `0.1.0-6e0fb907` (2026-10-15); host steps 1-8 done the same day,
  review `docs/reviews/2026-10-15-long-dev-first-look.md`.

**Host steps after release 4b** (who runs each in brackets; all done 2026-10-15):

1. [agent] `zqr fetch-data --product BTC-USD --granularity 1h --start 2015-07-20 --end 2021-01-01` and the same
   for ETH-USD from 2016-05-18 (the probe's 88,000 bars; resumable).
2. [agent] `zqr extend-dataset --dataset coinbase-btc-usd-one_hour-0d7202d6b155 --start 2015-07-21 --fill-policy
   ffill_flat` and ETH from 2016-06-01 (F1); report coverage, gaps and the junction.
3. [agent] `zqr create-split --dataset <new> --like split-1016c7c9873a` (BTC) and `--like split-c47a0be57a24` (ETH).
4. [human] `zqr split-lineage --split <new BTC> --parent split-1016c7c9873a --note ...` and the same for ETH (also
   closes the new holdouts).
5. [human] write the two mandates (F3), `zqr mandate create --file ...`, `zqr desk use-mandate --product ...
   --mandate ... --note ...`.
6. [agent] `zqr memory characterise` and `zqr memory run --sandbox docker` (E1.5).
7. [agent] `zqr portfolio seed BTC-USD` and `ETH-USD` on the new mandates, `zqr portfolio dev <vid> --reference`.
8. [agent] `zqr portfolio periods` and `zqr library periods`; a review for the human,
   `docs/reviews/<date>-long-dev-first-look.md`: the early years against 2021-2024 for each seed family and the
   seed portfolios, buy and hold per period. Research stays disabled; Tasks 3-16 are then detailed on the long dev.

## Open points to settle while detailing the tasks

- Family quotas per split (the design names them, no values yet).
- Campaign model choice and per-session wall clock.
- Shadow traders start from the release day's bar, cash, like any new paper trader (decided with release 4a).
