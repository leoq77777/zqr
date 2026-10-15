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

Tasks 3-16 below are the outline as approved; they are detailed (2026-10-15, on the long dev) in the sections
"Release 4c" and "Release 4d" further down, which replace them where they differ.

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

- Family quotas per split (the design names them, no values yet). Proposal P1 below.
- Campaign model choice and per-session wall clock. Proposal P2 below.
- Shadow traders start from the release day's bar, cash, like any new paper trader (decided with release 4a).

## Release 4c: registered hypotheses and the two gates (human CLI, no agent)

Approved 2026-10-15 with proposals P1-P6 as written. The gate details are appendix A13.7; P6 is in the investment
policy, section 3.

**Goal.** A human registers a hypothesis on a product's current mandate split; one controller command runs gate 1
and, if a variant passes, gate 2; a passing version becomes `admitted` and waits for the next split. No agent and
no campaign: research stays disabled. Release 4d's agent calls the same functions.

**Facts this plan rests on** (read in the code on 2026-10-15):

- Gate 1's test is `evaluation/split_stats.appraisal_test(candidate, portfolio, k)` (A6), implemented and unused.
- Gate 1's reference is `portfolio/evaluate.gate1_reference(store, product, split_id)`, recorded by
  `zqr portfolio dev <vid> --reference` (BTC `pv-bef1798ef4d9`, ETH `pv-77bad34a29c6` on the long-dev splits).
- Research measures use the run without the ladder (A13.6): `research_equity_file`, `sharpe_daily_no_ladder`,
  `max_drawdown_no_ladder`; `turnover` is in the run summary.
- `portfolio/blend._targets` refuses `plugin:` members ("phase 3b"); `strategy/compute.compute_targets` already
  runs plugins through `run_plugin` with a `SandboxConfig`.
- Version states have no `admitted`; transitions out of `draft` are `validated` / `failed_validation`
  (controller).
- Library entries come only from the seed catalog (`LibraryStore.import_catalog`); statuses `candidate`,
  `probation`, `member`, `retired`. Families carry `sleeve: bool`; members share capital equally across sleeves
  and equally within a family, capped at `member_cap` 0.15.

**Scope limits (4c and 4d).**

- A hypothesis is on one product and its variants are sleeve members (template or plugin) in one family.
- Overlay hypotheses are refused: a different overlay changes the reference itself; it would be proposed to the
  human as a reference change, not run through the gates.
- Plugin members run in backtests through the docker sandbox; paper refuses plugin members until a later phase
  (no version reaches paper before the next split, earliest 2027-10-12).

### C1. Hypothesis store and registration

Files: new `src/quant_research/hypothesis/__init__.py`, `model.py`, `store.py`, `commands.py`; `cli.py`;
`tests/test_hypothesis.py`.

- [x] `model.Hypothesis` (pydantic, frozen, `extra="forbid"`): `product`, `family`, `prior` (1-2,000
      characters), `citations` (at least one; each `{kind: "published"|"experiment", ref}`),
      `variants` (1-3 of `{strategy_ref, parameters}`), `campaign_id` (optional). Id `hy-` + hash of the body plus
      the split and reference (below).
- [x] `store.HypothesisStore` on the experiments database (like `VersionStore`): tables `hypotheses` (id, product,
      split_id, mandate_id, reference_id, family, campaign_id, body, registered_by, note, at) and
      `hypothesis_events` (append only: registration, gate results). No update or delete path.
- [x] Registration fixes the split (the product's desk mandate split), the mandate and the reference
      (`gate1_reference`) and records them, so a later mandate change does not move a registered hypothesis.
- [x] Refusals, each with its own message: no mandate or no reference for the product; 0 or more than 3 variants;
      two variants with the same normalised spec; unknown family or a family without a sleeve; unknown template or
      invalid parameters; a plugin hash not registered; an experiment citation that does not exist or is not a dev
      experiment on the hypothesis split (or a split it extends, A13.4); a second registration with the same
      `campaign_id`; an identical body already registered.
- [x] State derived from events: `registered` → `gate1_failed` | `gate2_failed` | `admitted`.
- [x] CLI (human): `zqr hypothesis register --file h.yaml --note ...` (plugin variants as `plugin_file: path`,
      frozen with `strategy/plugin.register_plugin` to `plugin:<sha256>`); `zqr hypothesis list [--product]
      [--state]`; `zqr hypothesis show <hid>` (body, events, gate evidence).
- [x] Tests: every refusal; the stored body is the registered body; a plugin file edited after registration does
      not change the variant.

### C2. Plugin members in portfolio backtests

Files: `portfolio/blend.py`, `portfolio/backtest.py`, `portfolio/evaluate.py`, `portfolio/paper.py`,
`tests/test_portfolio.py`.

- [x] `blend(version, bars, paths, sandbox=None)`: `_targets` passes `plugin:` refs to `compute_targets` with the
      sandbox; a plugin member without the docker sandbox is refused.
- [x] `run_segment` / `record_run` / `run_dev` / `validate` take the sandbox setting from the caller;
      `zqr portfolio dev|validate` take `--sandbox docker` (default none), `zqr hypothesis test` defaults to docker.
- [x] Paper refuses a version with a plugin member or overlay, with the reason: `approve` and `shadow` check
      before the roster entry is written (the trader's blend has no sandbox and would refuse it anyway).
- [x] Tests: a fake sandbox runner; template-only versions run unchanged (the existing portfolio tests).

### C3. Candidate library entries and families

Files: `library/store.py`, `library/commands.py`, `portfolio/seed.py`, `tests/test_hypothesis.py`.

- [x] `LibraryStore.add_candidate(product, family, strategy_ref, parameters, source, actor)`: entry id
      `<product base>-<family>-h<hash8>` (like the seed ids), status `candidate`, source `hypothesis:<hid>`;
      idempotent on identical identity. Templates keep `template_version`; plugins carry the hash in the ref.
- [x] `LibraryStore.add_family(family, note)` and `zqr library family add` (human). A new family is how a
      hypothesis outside the four trend sleeves becomes registrable.
- [x] The seed portfolio takes seed-catalog entries only (source `seed`), so a gate 2 candidate never changes
      `zqr portfolio seed`.
- [x] Tests: idempotence; the seed portfolio is unchanged by a candidate; family add is unique and needs a note.

### C4. Gate 1: marginal value on dev

Files: new `src/quant_research/hypothesis/gates.py`; `tests/test_hypothesis.py`.

- [x] `variant_run(paths, store, hid, index, sandbox)`: one dev experiment of the variant alone, with the
      reference version's engine config (costs, cash, long only) and the mandate's policy, on the hypothesis
      split; kind `registered` (not in `NON_TRIAL_KINDS`), so it counts as a trial on the split; plugins need
      docker (`allow_weak_isolation=False`). Reruns of a recorded variant reuse the experiment.
- [x] `gate1(paths, store, hid, sandbox)`: for each variant, daily returns on the A1 grid of the variant run
      (`R_c`) and of the reference's dev run without the ladder (`R_p`), aligned on common days;
      `appraisal_test(R_c, R_p, k=len(variants))`. Event `gate1` with per-variant `beta`, `alpha_daily`,
      `appraisal_ratio`, `statistic`, `passed`, experiment ids. The best passing variant (highest AR, A6) goes
      on; none passing ends the hypothesis (`gate1_failed`, A6: recorded, not adjusted and retried).
- [x] Diagnostic (proposal P4): the hedged series' Sharpe per period, the review's cuts (BTC 2015-2020 /
      2021-2024, ETH 2016-2020 / 2021-2024), stored with the event; it does not decide.
- [x] `marginal(paths, store, experiment_id)`: the same regression for any completed dev experiment on the split
      against the reference, without a verdict and without registration (4d's gateway tool; humans via
      `zqr hypothesis marginal <experiment>`). It reads only dev data. In 4c it records nothing; counting the
      agent's calls as trials is part of D5.
- [x] Tests: synthetic `R_c = 0.5 R_p + noise` fails, `R_c` with independent positive drift passes at the bar for
      its K; K follows the variant count; the reference's no-ladder file is the one read.

### C5. Gate 2: do no harm on validation

Files: `hypothesis/gates.py`, `portfolio/version.py`, `portfolio/evaluate.py` (`turnover_no_ladder`),
`tests/test_hypothesis.py`.

- [x] `gate2(paths, store, hid, sandbox)`: the passing variant becomes a candidate library entry (C3) and a new
      version = the reference's members plus this member at full weight (status `member`), parent = reference,
      blended by the normal rules (a new family becomes a new sleeve). Both versions run on validation; measures
      without the ladder (A13.6).
- [x] Rule (outline Task 5): candidate Sharpe ≥ reference Sharpe, candidate max drawdown at most 2 points worse,
      candidate turnover at most 25% higher (turnover of the run without the ladder, a new metric; a validation
      run recorded before 4c lacks it and gate 2 refuses to compare it). Each validation run is recorded in
      `validation_uses` (the reference's once per split, the candidate's every time).
- [x] Event `gate2` on the hypothesis with both sets of measures and the verdict; `gates.verdict` is the
      agent-facing result (state and failed gate only).
- [x] Tests: each of the three conditions failing alone; validation uses counted; repeated `gate2` on the same
      hypothesis refused.

### C6. Admitted versions

Files: `portfolio/version.py`, `portfolio/commands.py`, `hypothesis/commands.py`, `desk/report.py`, tests.

- [x] State `admitted` (not terminal): `("draft", "admitted")` and `("draft", "failed_validation")` by the
      controller with the gate evidence. `admitted` leads nowhere in phase 4; at the next split the version is
      re-run there (design D5), outside this phase.
- [x] `zqr hypothesis test <hid> [--sandbox docker|none]` (controller; anyone may trigger it): gate 1 then, if a
      variant passes, gate 2; prints the hypothesis state and, for the human, the measures.
- [x] `zqr portfolio list --state admitted` (existing filter); the daily report's research section lists every
      hypothesis with family, state, gate 1's best appraisal ratio and the version, and the next split date when
      a version is admitted.
- [x] Tests: the state machine; `admitted` cannot be frozen, approved or shadowed.

### C7. Docs, suite, rehearsal, release 4c

- [x] `docs/development.md`: hypotheses, gates, admitted, new commands (section 5.10); the plan's checkboxes.
- [x] Appendix A amendment A13.7 (approved with the plan): gate 2's numbers as above and the reference's
      validation run as a counted use; the per-period diagnostic of P4.
- [x] Full suite (329 passed), ruff; rehearsal on a copy of production (2026-10-15, read-only backup of the
      database plus snapshots, splits, library and the two reference dev runs; hypotheses drafted by the agent,
      copy only):
      - BTC `tsmom` daily lookbacks 24 h and 72 h (K = 2): T = 3,258 days, β 1.17 / 1.24, AR −3.43 / −1.10,
        `gate1_failed` (costs of daily trading).
      - ETH `breakout` 168/48 (K = 1): T = 2,942, β 1.29, AR 0.22 against the bar 0.451, `gate1_failed`; hedged
        Sharpe 0.94 before 2021 and −1.14 from 2021, the pattern P4 is meant to show.
      - BTC `breakout` 480/240 with gate 1 forced to pass in a script (to exercise gate 2 on real validation
        data): reference and candidate validation runs without the ladder (Sharpe 1.094 → 1.117, drawdown
        −19.2% → −19.3%, turnover 6.04 → 6.03), validation uses 0 → 2, library entry
        `btc-channel_breakout-hf35f614f`, version `admitted`, verdict without measures, report table correct.
      - Observation: a fourth member in a family of three changes the blend little (its share is a quarter of
        one sleeve), so gate 2 is a weak filter for variants joining an existing family; gate 1 carries the
        evidence there, as A6 intends.
- [ ] Ask before merging and releasing. After release, hypotheses on production are registered by the human.

Caveat for 4c: the human has seen the seed portfolios' validation results (seed review), so gate 2 on
human-registered hypotheses is a harm check, not independent evidence. The next split's holdout remains the
independent test.

## Release 4d: campaigns, budget and the research loop

**Goal.** Research sessions run campaigns from a reviewed queue, register at most one hypothesis per campaign,
and the gates of 4c judge it. Budget is reserved before every session; the loop no longer promotes single
candidates (D8).

### D1. Campaign record and store

Files: new `src/quant_research/campaign/__init__.py`, `store.py`, `commands.py`; `cli.py`; tests.

- [x] Fields of design 6.1: id, product, split, family (or `exploration`), goal, prior with citations, source
      (`generator` with its diagnostic, or `human`), model, caps (P3), state, created/updated.
- [x] States: `proposed` → `queued` (human approval, P5) → `running` → `concluded` | `cancelled`; `exhausted` when
      a cap is reached without a conclusion. One campaign `running` at a time.
- [x] CLI: `zqr campaign list/show`, `add --file` (human; a prior with citations is required, D6), `approve`
      (human), `cancel --note` (human).

### D2. Generator and family quotas

Files: new `campaign/generate.py`; tests.

- [x] Inputs, dev data only (D6): the reference version's dev run without the ladder (period statistics,
      `family_contribution`), the family archives (`memory/archive.build_archives`), conclusions, trials per
      family on the split.
- [x] Proposals: regime gaps (periods where the reference's hedged contribution is weakest), concentration (all
      four sleeves are trend; the reference's return is mostly one family), cost (archive `sharpe_2x` falling
      against `sharpe`), open questions in conclusions; exploration about 20% of each batch.
- [x] Ranking: gap size × room left in the family quota; deterministic for the same inputs (tested).
- [x] Quotas (P1): trials per family per product split counted from the split's own trials (inherited trials not
      counted); a family at quota gets no campaign; registration is refused once a campaign's family is at quota.
- [x] `zqr campaign generate --product [--n 6]` writes `proposed` campaigns and prints them.

### D3. Budget reservation (D7)

Files: `desk/research.py` (session loop and research ledger), `desk/config.py`, tests.

- [x] Before a session: reserve `session_reserve_usd` (P2) against the campaign, the day ($5) and the month
      ($83.33); refuse if any does not fit; release the difference after the session from the booked cost.
- [x] A model call without a configured price stops the loop with the model name in the report (`_cost` no longer
      books "unpriced" as $0).
- [x] Campaign top-ups: +$2 after a gate 1 pass, +$2 after a gate 2 pass, at most $5; caps in sessions and trials
      grow with them (P3).
- [x] `daily_cost_cap_usd` default 5. Tests: refusal at each cap; an overrun beyond the reservation stops the
      loop; unpriced stops.

### D4. Loop change

Files: `desk/research.py`, tests.

- [x] `step` takes the running campaign, or starts the next `queued`; idle with no model calls when none.
- [x] Session brief: campaign goal and prior, family archive (agent view), one-line summaries of other families,
      earlier session notes of the campaign, remaining budget in dollars, sessions and trials.
- [x] Progress notes: each session writes `notes` into `research.json`; the controller appends them to the
      campaign.
- [x] Remove topic rotation (`TOPICS`, `pick_topic`) and single-candidate promotion (`_evaluate`) from the loop
      (D8); the single-candidate code stays for controls and benchmarks.
- [x] After a session that registered a hypothesis, the controller runs `zqr hypothesis test` logic (with the
      docker sandbox) before the next session.

### D5. Gateway tools

Files: `gateway/service.py` (`ToolGateway`), `gateway/mcp_server.py`, tests.

- [x] `family_archive(family)`: the agent view (dev only, holdout-viewing counts).
- [x] `portfolio_members()`: the reference's members with dev statistics.
- [x] `marginal_eval(experiment_id)`: C4's `marginal`, counted as a trial.
- [x] `register_hypothesis(...)`: C1 with the campaign id; plugin variants from workspace artifacts frozen as the
      gateway already freezes final artifacts. The result of the later gates reaches the agent as verdicts only.
- [x] Tests: no validation or holdout number in any tool output (scan of the payloads).

### D6. Conclusions and archive write-back

- [x] The campaign's last session (concluded, exhausted or after the gates) writes a conclusion through
      `memory/conclusions.py`; citations checked (experiment ids exist on the split and are dev).
- [x] The conclusion record is appended to the family archive (dev data and gate verdicts only).

### D7. Report

- [x] Research section: campaigns by state, registrations and gate verdicts, admitted versions, budget used against
      the day and month caps, sessions and trials used against quotas, cost per conclusion.

### D8. Debts

- [x] Topic tagging refuses to run without `desk/research_ledger.jsonl` (it cleared tags in the phase 3
      rehearsal).
- [x] The rejected-proposals section of the clustering report lists the clusters' sizes, members and the reason.

### D9. Tests, docs, rehearsal, release 4d and checkpoint

- [x] FakeAgent end to end: several sessions of one campaign, registration, both gates, conclusion, citation check,
      archive record; quota exhaustion; cap refusals; closed-holdout refusal; shadow trader replay
      (`tests/test_campaign.py`, 27 tests; on a closed split campaigns and gates run and only holdout runs are
      refused (D1, D3), since both desk splits inherit the 4a closures; 4d does not touch paper, so the shadow trader stays covered by
      `tests/test_portfolio_paper.py`'s replay and parity tests).
- [x] Docs: `docs/development.md` (sections 5.1, 5.11, configuration, commands), the design's campaign sections
      cross-referenced; no appendix rule changes (interpretations below, for the human's review).
- [x] Full suite, ruff, rehearsal on a copy of production with the fake agent and one real session on the
      configured model (cost booked). 350 passed. Rehearsal 2026-10-15 on a read-only backup of production
      (`/tmp/zqr-4d-rehearsal`): `campaign generate` gave 6 proposals per product; the fake agent ran a BTC `tsmom`
      campaign through the real loop and gates (explore, register, gate 1 failed with AR -3.0, conclusion,
      archive appended, report); one real session (deepseek-flash, Claude Code 2.1.112, docker isolation) on a BTC
      `ewma_trend` campaign ran 12 experiments and 6 marginal evaluations, registered two plugin variants, gate 1
      failed in the docker sandbox; 0.066 USD booked against the 0.25 reservation, audit clean, no violations,
      no containers left. Findings, fixed: (1) campaigns were refused on closed splits, but both desk splits inherit
      the 4a closures and the closure only stops holdout runs; (2) every regime gap went to the alphabetically first
      family (equal quota room), and bull years dominated the absolute shortfall: now a log shortfall, years go round
      the families; (3) the brief cut the family archive at 8,000 characters across both products, losing the
      campaign's own split: now the product's sections, own split first; (4) a CLI cost estimate without a price
      was booked as priced; now it is `unpriced` and stops the loop; (5) report tables ran into the next list line;
      (6) unknown price presets in `campaign_models` are refused when the config loads.
- [x] Ask before merging and releasing. After release: `zqr campaign generate` for both products; the human
      reviews and approves the queue, then sets `research_enabled = true`. Released `0.1.0-89f03f10`
      (2026-10-15); 6 proposals per product generated on production the same day (the rehearsal's batch).
      The human delegated approval of all 12 to the agent; queued by score with the products interleaved, and
      `research_enabled` set to true the same day. First production session (ETH `ewma_trend`): 12 experiments,
      a registration, gate 1 failed, 0.064 USD, audit clean.

### Interpretations taken while building 4d (for the human's review)

- **Exploration quota.** "Exploration capped at 25%" (P1) is 25% of the sum of the sleeve families' quotas
  (0.25 × 100 × 4 = 100 trials per split), not a share of campaign trials (a share would refuse the first
  exploration campaign).
- **Caps are ceilings in code.** Day $5, month $83.33 and 12 sessions per day are constants (`campaign/budget.py`
  `CEILINGS`); the desk config may only lower them. Production's config still holds 20 and 200 from before.
- **After the gates.** A campaign continues after its gates on the top-ups; it ends when a session writes a
  conclusion or a cap is reached. Design 6.3 ends a campaign once its hypothesis is tested; D7's top-ups after
  gate passes only make sense if it goes on, so the code follows D7 (to confirm). On a cap without a conclusion the controller writes an `inconclusive` one from
  the last notes.
- **Last session.** The brief asks for a conclusion when at most one session is left or the dollars left after
  this reservation cannot pay another.
- **Uncounted sessions.** Environment failures, harness errors and crashes, and stops do not count against the
  session cap (their cost still does).
- **`marginal_eval`** counts each distinct experiment once as a campaign trial.
- **Conclusions** of campaigns are `family_conclusions` rows with session id `campaign:<id>`; the benchmark's
  `research.schema.json` is unchanged, the desk workspace's copy adds `notes` (required) and `conclusion`.
- **Not built:** automatic generation after each campaign or daily; `zqr campaign generate` is manual.

## Proposals (approved 2026-10-15)

**P1. Family quotas per product split.** 100 trials per family per product split (four trend sleeves: 400 per
product); exploration (new or non-sleeve families) at most 25% of a split's campaign trials. A family at quota gets
no campaign and no registration until the next split. Reasoning: the split already inherits K 231 (BTC) and 240
(ETH) from about 900 earlier trials; gate 1 does not depend on K, but more searching inside one family on one dev
mostly overfits that dev. 100 is about ten sessions at the ledger's median of 10 experiments per session.

**P2. Model and session settings.** Default model: the configured `ANTHROPIC_MODEL` (deepseek-flash today: 183
sessions, median $0.027 per completed session, p90 $0.054, max $0.075). A campaign may name another model from
`campaign_models` in the desk config, each with a price; no price, no session. `session_reserve_usd` 0.25 for
deepseek-flash (three times the largest observed session). Wall clock stays 1,800 s per session; the 4d rehearsal
reports how many sessions reach it. `max_sessions_per_day` lowered from 200 to 12.

**P3. Campaign caps in sessions and trials as well as dollars.** At deepseek-flash prices $1 buys about 35
sessions, so dollars alone do not limit anything. A campaign starts with $1, 4 sessions and 40 dev experiments;
each top-up (D7) adds $2, 2 sessions and 20 experiments; at most $5, 8 sessions, 80 experiments. Whichever cap is
reached first ends the campaign (`exhausted`) and the last session writes the conclusion.

**P4. Gate 1 per-period diagnostic.** Yes: the hedged series' Sharpe per period is recorded and reported with
gate 1 (dev data, so the agent may see it in 4d). It does not change the verdict; a variant whose whole
appraisal comes from the early years is flagged in the report for the human.

**P5. Queue approval.** Every generated batch enters `proposed` and needs `zqr campaign approve` by the human,
not only the first queue. At 12 sessions a day and 4 sessions per campaign a batch of 6 lasts about two days.
Revisit after the first month.

**P6. Resume after liquidation (investment policy section 3; needs approval, no code in 4c or 4d).** Today
`zqr portfolio resume` has no conditions. Proposed: not earlier than 30 days after the liquidation; a written
review in `docs/reviews/` before resuming (what caused the drawdown, whether the version's thesis still holds,
whether the market regime is one the version's dev covered); otherwise the version stays halted or is retired.
On the long dev both seed portfolios were liquidated by the ladder (BTC 2020-03-12, ETH 2023-09-11); without it
their worst drawdowns were −39% and −33%, so this rule is not hypothetical.

## After 4d: strategy inbox (approved 2026-10-15)

Design `docs/superpowers/specs/2026-10-15-strategy-inbox-design.md`, decisions I1-I6 approved in conversation;
the human asked to build and merge it directly. I6 is the plan decision recorded here: inbox drafts are written as
`proposed` campaigns, like `zqr campaign generate`, so approval stays the human's (P5) and no appendix rule changes.

- [x] `inbox/` (store, drafting workspace, draft → campaigns, report lines, commands), loop integration, config
      (`inbox_reserve_usd` 0.10, `inbox_drafts_per_day` 10, `inbox_wall_seconds` 600), docs (section 5.12).
- [x] `tests/test_inbox.py` (13 tests); full suite 364 passed, ruff.
- [x] Rehearsal on the 4d production copy with the real model (deepseek-flash, Claude Code 2.1.112, docker
      isolation): a Chinese Weibo post on a 4-hour Bollinger squeeze breakout became two `proposed`
      `channel_breakout` campaigns (BTC, ETH) that restate the rule on hourly bars, call the author's 85% a claim,
      and name the existing archive and campaigns of the family; a funding-rate carry post was `infeasible`
      (perp short, funding data). 0.024 USD for both, no violations, no containers left; report and status lines
      checked.

## After 4d: reassessment fixes (2026-10-15)

Review `docs/reviews/2026-10-15-reassessment/zqr-reassessment.md` (for the human only; its probe script reproduces
the defects as they were and fails on the fixed code). The human approved fixing R1-R4 without contract changes
and drafting R5 for approval.

- [x] R1: a portfolio order target stays open (`DeciderState.open_target`, ledger key `portfolio_open_order`)
      until an order fills it in full without the `max_order_notional` cut; capped, rejected and lost orders are
      re-issued; the backtest never opens a target, parity re-runs the decider from `pre_state` as before.
- [x] R2: the family quota per action (`CampaignStore.trials_left`): session allowance, each experiment and each
      `marginal_eval` re-checked live; a registration needs room for all its variants.
- [x] R4: a run with no usage that may have called the model is `cost_unknown`, booked at its reservation, and
      stops the loop; an unpriced run without the CLI's estimate is booked at its reservation too.
- [x] R3: `zqr portfolio resume` enforces P6: 30 days from the recorded liquidation and a `docs/reviews/` review
      carried with the request (`--stdin` on hosts), re-checked by the trader at the next bar.
- [x] Tests converted from the probes; full suite 372 passed, ruff.
- [x] R5: amendment A13.8 (draft `docs/superpowers/specs/2026-10-15-a13-8-extreme-prints-draft.md`), approved
      2026-10-15 after the sensitivity comparison (`docs/reviews/2026-10-15-a13-8-sensitivity/`): prints kept,
      isolated opens corrected (the human revised decision 1 when the comparison showed signals reading the ETH
      open), the guard only at isolated opens, revalidated lineage, family quotas over ancestors. Code and tests;
      full suite 375 passed, ruff.
- [ ] R5 migration on the host, after the release (the rehearsal on a production copy produced the dataset ids
      below; the ids are content hashes, so the host should reproduce them):
  1. `zqr research stop`; let a running campaign session end.
  2. Agent: `zqr extend-dataset --dataset coinbase-btc-usd-one_hour-0d7202d6b155 --start 2015-07-21
     --fill-policy ffill_flat` (expect `coinbase-btc-usd-one_hour-f9799521097f`) and the same for ETH from
     `coinbase-eth-usd-one_hour-8e0c6c544c93`, `--start 2016-06-01` (expect `coinbase-eth-usd-one_hour-d46750883306`);
     `zqr create-split --dataset <new> --like split-f0d7a178e08b` (BTC) and `--like split-d6a76022a19d` (ETH).
  3. Human: `zqr split-lineage --split <new> --parent <current long-dev split> --note "A13.8"` (kind
     `revalidates`); new mandates on the new splits; `zqr desk use-mandate`.
  4. Agent: `zqr memory characterise`, `zqr memory run --sandbox docker`, seed portfolios,
     `zqr portfolio dev <vid> --reference`.
  5. Human: cancel queued campaigns and re-approve their replacements on the new splits (a running one finishes on
     the old split).
  6. Agent: first-look review, old against new long dev for the seeds and references.
