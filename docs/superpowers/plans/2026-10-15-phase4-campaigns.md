# Phase 4: campaigns and admission gates — plan (draft for review)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** D1-D8 approved by the human on 2026-10-15. Tasks 0-2 (holdout closure, shadow portfolios) ship first
as release 4a so the shadows start accruing forward evidence. Tasks 3-16 are an outline; they get detailed steps
after the data decisions raised on 2026-10-15 (longer history, see the end of this plan), which change the dev
segment gate 1 runs on.

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

Options, to decide before Tasks 3-16 are detailed:

- **E1. Extend dev backwards (proposal).** Fetch from the listing dates; freeze new datasets (the current ones stay);
  create new splits per product with dev from the listing date to 2024-06-20 and the same validation and holdout
  boundaries, closed at creation (A13.3, the same data); new desk mandates written by the human. An amendment says
  how the old splits' trials count on the new ones (proposal: the old split's K(0.7) is added to the new split's K,
  since that search covered part of the new dev). Seed characterisation runs (A7) and the seed portfolios' dev runs
  are repeated on the new dev; the 2015/16-2020 part is reported separately as the first look at those years. Gate
  1 and campaign diagnostics then run on the long dev.
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

## Open points to settle while detailing the tasks

- Family quotas per split (the design names them, no values yet).
- Campaign model choice and per-session wall clock.
- Whether shadow traders start from the release day's bar or from a full replay of history (proposal: from the
  release day, cash, like any new paper trader).
