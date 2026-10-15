# Phase 3: portfolio layer — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One blended position per product. Build immutable portfolio versions, a blending engine with sleeves,
the member cap and the volatility overlay, a stateful decision step (signal deadband and drawdown ladder) shared by
backtest and paper, the seed portfolio and its validation run, a holdout gate for versions, one paper portfolio
trader per product, portfolio parity and a report section. Stop at the human checkpoint: the seed portfolio's
validation result, before any holdout viewing.

**Rules:** design `docs/superpowers/specs/2026-10-13-research-library-portfolio-design.md` sections 5.1-5.3, 5.5,
5.8, 5.9; `docs/contracts-v2-appendix-a.md` A6 (seed portfolio and gate 3), A8 (deadband), A9 (holdout budget);
`docs/investment-policy.md` sections 1-4 and 6. The decisions D1-D8 below must be approved before Task 1. Task 0
then writes them into those documents.

**Architecture:** The `quant_research/portfolio/` package grows from one policy module to a layer:
- `version.py`: the immutable version and its store;
- `blend.py`: a pure, vectorised blend of the member targets;
- `decide.py`: the decision state machine, the only stateful step;
- `backtest.py`: runs a version through the existing engine via a decider hook;
- `evaluate.py`: dev and validation runs.

The holdout run of a version lives in `validation/holdout.py`, the only module allowed to read holdout data. Paper
adds a `PortfolioTrader`, a subclass of `PaperTrader` that replaces target computation and order gating. Parity,
the daily report and a monthly report read the decision records. The single-strategy path and the 16 control
traders are unchanged.

**Tech stack:** Python 3.12, numpy, pandas, pydantic, SQLite. No new dependencies.

**Branch:** `phase3` from `main` (`6586f94`).

---

## Decisions for the human (approve before Task 1)

Each item states the proposal and the reason. Items D1-D3 settle the investment policy's "To define (phase 3)"
points. D4-D7 fill gaps the design leaves open. D8 splits the phase.

- **D1. Order cap (investment policy 1).** Portfolio traders use `max_order_notional` = the portfolio's capital
  ($10,000). A full move from cash to invested is then one order, and the backtest needs no clip, as with the
  controls since 2026-10-15. A lower per-order cap for live trading is decided with live trading, which is out of
  scope; if one is introduced, the backtest must model the same clip.
- **D2. Soft stop (investment policy 2).** "Significantly below" means the following:
  - the measure is the trailing 12-month annualised Sharpe of the combined account minus that of the 50/50 buy and
    hold (same capital, same costs), from daily marks;
  - the stop triggers when that difference is below −0.5;
  - it is evaluated on each monthly review day (the 1st, UTC) once 12 months of paper exist.

  When it triggers, `desk/soft_stop.json` is written, the research loop refuses to dispatch, and the report asks
  for the human review. Only `zqr desk soft-stop clear --note` removes it.

  A significance test would almost never trigger, because the Sharpe standard error over 12 months is about 1.0.
- **D3. Drawdown ladder (investment policy 3).**
  - **Measured on:** each product portfolio's own paper equity. The two portfolios are independent accounts with
    independent orders. The combined account's drawdown is reported, with the yellow flag only.
  - **Peak:** the high-water mark of the bar-close equity since the product portfolio started paper.
  - **Levels:** at −20% a yellow flag; at −25% the portfolio is halved (target × 0.5); at −30% it is liquidated
    (target 0) and halted.
  - **Recovery:** a halved portfolio returns to full sleeves when its drawdown recovers to −15% or better. The
    10-point gap stops it switching back and forth.
  - **After liquidation:** only `zqr portfolio resume <name> --note` (human) re-enables trading. Resume resets the
    high-water mark to the current equity; otherwise the −30% rule would fire again at once.
  - **Orders:** a change of ladder level always trades, like a full exit, even inside the deadband.
- **D4. The ladder is part of every version and runs in backtests too.** Dev, validation, holdout and paper use one
  function (`portfolio/decide.py`). This does not change the seed validation verdict. The ladder acts only at
  −25% or worse, so a portfolio whose drawdown never reaches −25% runs identically with or without it. One that
  does reach −25% fails the A6 standard (maximum drawdown no worse than −25%) either way. The report also shows the
  drawdown without the ladder.
- **D5. Portfolio dev runs count as trials.** Each version's dev run is recorded as an experiment of kind
  `portfolio`. It counts as a trial on the split and enters the return archive (no family tag), consistent with
  A8: changing the construction later counts as a trial. The seed portfolio adds one trial per split.
  - Validation runs (kind `portfolio_validation`) and holdout runs (kind `holdout`) are not trials.
  - Each validation run is counted per split in a new `validation_uses` table and shown in the report (A6).
- **D6. Version transitions after validation are human-only in this phase:** `zqr portfolio freeze`, `holdout`,
  `approve`. The standing authorization covers single candidates, not portfolio versions. The design's
  checkpoint requires the human to see the seed validation result before the holdout.
- **D7. Gate statistics use A4.**
  - Sharpe in the validation standard and in gate 3 is the A4 daily Sharpe (`split_stats.sharpe_daily` on the
    version's daily returns). The legacy hourly Sharpe is not used.
  - Maximum drawdown is measured on bar-close equity.
  - Total return is final over initial equity.
- **D8. Split the phase.**
  - **3a (this plan, Tasks 0-13):** the seed portfolio, from definition to paper.
  - **3b (outline at the end):** admission gates 1-2 with registration, version switching on the same paper
    account, probation and retirement proposals. These are needed only once new members exist, which happens with
    campaigns in phase 4, so 3b is written as its own plan after the seed portfolio is running.

**Precondition (investment policy 6):** the account's real Coinbase taker fee tier, checked by the human on the
Trade Fees page. The plan assumes 0.6% taker plus 5 bps. Some published schedules list a higher taker rate for the
smallest volume tier, so this check matters. If the real rate is higher, `fee_rate` changes in the desk mandates
before Task 6. The new mandate versions are written by the human, and the seed portfolio is defined on them.

## Facts this plan relies on (checked in the code on 2026-10-15)

- **Library and seed families.**
  - The library holds 12 seed entries × 2 products (`library_entries`, status `candidate`).
  - The four sleeve families and their members per product: `tsmom` (4: 1w, 1m, 3m, 12m), `ewma_trend` (3),
    `channel_breakout` (2), `trend_filter` (2). `vol_overlay` is `sleeve: false`, with one overlay entry,
    `voltarget-1m`.
  - The catalog's `max_member_share` is 0.15.
- **Member shares under equal sleeves (F = 4, 25% each):** tsmom 6.25%, ewma 8.33%, breakout 12.5%, trend
  filter 12.5% per member. All are under the 15% cap, so no sleeve leaves cash unused.
- **Template outputs.** Seed templates return exposures in [0, 1] on every bar. Cadence templates hold their value
  between boundaries (`strategy/cadence.py` `sample_and_hold`). There is no NaN after warm-up; warm-up gives 0.
  `vol_target` returns the overlay scale min(1, 0.40 / σ of 30 daily returns).
- **Warm-up on dev.** The dev window starts 2021-01-01 together with the data. Members with long lookbacks
  (`tsmom-12m`, `faber-10m`, the EWMA normalisation over a year) hold 0 for most of 2021, so the seed portfolio's
  dev result includes a partly invested first year. Validation and holdout have full history behind them.
- **Engine.** `backtest/engine.py` `run_backtest` decides at bar `i-1-delay` and fills at bar `i`'s open; it
  compares the target with the drifted weight through `needs_trade(tolerance=rebalance_tolerance)`. The portfolio
  path needs a target that depends on the account state (last executed target, equity for the ladder), so the
  engine gets an optional decider hook. Without a decider its behaviour is unchanged, and so is `ENGINE_VERSION`.
- **Holdout.**
  - `validation/holdout.py` is the only reader of holdout data; a test enforces it (`1f66b84`).
  - Holdout viewings are rows in `holdout_runs(split_id, candidate_hash, experiment_id)`, capped by
    `split_viewing_cap` (desk mandates: 10).
  - No holdout viewing has been used on either current split.
- **Paper.**
  - The fleet builds `PaperConfig` from `RosterEntry` (`paper/fleet.py` `_config`). It refuses identity changes
    (`config_change_refused`).
  - The paper slot cap counts only `source = "candidate"` entries.
  - The bar store is seeded from the frozen dataset (since 2021), so every lookback is available.
- **Daily report.** It is written by `desk/commands.py` (`write_daily_report`); parity per entry comes from
  `paper/parity.py` `paper_parity`.

## File structure

| File | Responsibility |
|---|---|
| Create `src/quant_research/portfolio/version.py` | `Member`, `Overlay`, `Ladder`, `PortfolioVersion` (pydantic, frozen, hashed `pv-…`); `VersionStore` (tables below), lifecycle transitions |
| Create `src/quant_research/portfolio/blend.py` | `member_shares(version)`, `blend(version, bars, paths) -> Blend` (member targets, family targets, pre-overlay blend, overlay scale, target) |
| Create `src/quant_research/portfolio/decide.py` | `DeciderState`, `Decider` (deadband A8, ladder D3), `observe`, `decide`, `executed`, `resume` |
| Create `src/quant_research/portfolio/backtest.py` | `run_portfolio(version, bars, window, initial=None, state=None) -> PortfolioRun` (engine result + decision log) |
| Create `src/quant_research/portfolio/evaluate.py` | Dev and validation runs, experiment rows, results files, `validation_uses`, validation standard |
| Create `src/quant_research/portfolio/seed.py` | Build the seed version of a product from the library and the desk mandate |
| Create `src/quant_research/portfolio/commands.py` | `zqr portfolio seed / validate / show / list / freeze / holdout / approve / resume` |
| Create `src/quant_research/paper/portfolio_trader.py` | `PortfolioTrader(PaperTrader)`: blend target, decider state in the ledger, decision record |
| Create `src/quant_research/desk/softstop.py` | D2 evaluation, `soft_stop.json`, clear |
| Modify `src/quant_research/backtest/engine.py` | Optional `decider` argument (protocol `decide(i, equity, position) -> float`, `executed(i, target)`) |
| Modify `src/quant_research/validation/holdout.py` | `run_portfolio_holdout(paths, store, version_id, override_reason=None)`: cap, 7-day batching (A9), gate 3 |
| Modify `src/quant_research/experiments/store.py` | `portfolio_validation` in `NON_TRIAL_KINDS` |
| Modify `src/quant_research/paper/trader.py` | Extract `_target(history) -> TargetPortfolio \| None` and `_order_gate(...)` so the subclass overrides them; no behaviour change |
| Modify `src/quant_research/paper/fleet.py` | `RosterEntry.portfolio_version`, `source: "portfolio"`; `_config` embeds the version body; trader class by entry |
| Modify `src/quant_research/paper/parity.py` | `portfolio_parity(...)` |
| Modify `src/quant_research/desk/report.py` | Portfolio section, combined account, `write_monthly_report` |
| Modify `src/quant_research/desk/research.py` | Refuse dispatch while `soft_stop.json` exists |
| Modify `src/quant_research/desk/commands.py` | Monthly report on the 1st; `zqr desk soft-stop status / clear` |
| Modify `src/quant_research/cli.py` | Register `portfolio` commands |
| Tests | `tests/test_portfolio.py` (version, blend, decide, backtest, evaluate, seed, holdout), additions to `tests/test_paper.py`, `tests/test_fleet.py`, `tests/test_report.py`, `tests/test_desk.py`, `tests/test_experiments.py` |
| Docs | `docs/investment-policy.md`, `docs/contracts-v2-appendix-a.md` (A14), `docs/development.md` (section 5.9 Portfolio layer, commands, layout, test count) |

New tables in `experiments.db` (created by `VersionStore` with `CREATE TABLE IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS portfolio_versions (
    id TEXT PRIMARY KEY, product TEXT NOT NULL, split_id TEXT NOT NULL, parent_id TEXT, state TEXT NOT NULL,
    body TEXT NOT NULL, evidence TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, version_id TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
    actor TEXT NOT NULL, evidence TEXT, note TEXT, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS validation_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT, split_id TEXT NOT NULL, subject TEXT NOT NULL, experiment_id TEXT,
    at REAL NOT NULL);
```

`body` is the version JSON and never changes; only `state`, `evidence` and `updated_at` do, and every change is a
`portfolio_events` row.

Lifecycle: `draft → validated | failed_validation` (controller, deterministic standard), `validated → frozen`
(human), `frozen → final_evaluated | failed_final` (evaluator, through the holdout gate),
`final_evaluated → paper` (human), any non-terminal state → `retired` (human). Terminal states:
`failed_validation`, `failed_final`, `retired`.

---

## Task 0: Record the approved decisions

**Files:** `docs/investment-policy.md`, `docs/contracts-v2-appendix-a.md`.

- [ ] Replace the three "To define (phase 3)" paragraphs of the investment policy with the approved D1-D3 text,
  dated, keeping the phase 4 item as it is.
- [ ] Record the fee check result in investment policy section 6 (rate, tier, date). If it differs from 0.6%, stop
  here and ask the human for new mandate versions.
- [ ] Add appendix section A14 "Portfolio layer decisions (approved <date>)" with D4-D7: ladder in backtests, the
  trial counting of portfolio runs, human-only version transitions, A4 statistics in the gates. Update the status
  line.
- [ ] Commit: `Investment policy and appendix A14: phase 3 decisions`.

## Task 1: Portfolio versions

**Files:** Create `portfolio/version.py`; Test `tests/test_portfolio.py`.

- [ ] **Step 1: Failing tests**
  - `test_version_id_is_a_hash_of_the_body`: the same body gives the same `pv-` id; changing `band`, a member's
    parameters or the ladder changes it; `created_at` is not part of the body.
  - `test_version_store_records_every_transition`: draft → validated (controller) → frozen (human) →
    final_evaluated (evaluator) → paper (human); each step is a `portfolio_events` row; `frozen` by the
    controller and `validated → paper` are refused (`VersionError`).
  - `test_body_is_immutable`: there is no update path for `body`; inserting the same id twice returns the
    existing row.
- [ ] **Step 2: Run, expect FAIL** (`.venv/bin/pytest tests/test_portfolio.py -q`).
- [ ] **Step 3: Implement**

```python
class Member(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entry_id: str
    family: str
    strategy_ref: str
    parameters: dict[str, Any]
    status: Literal["member", "probation"] = "member"
    probation_start: str | None = None          # ISO date; phase 3b

class Overlay(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entry_id: str
    strategy_ref: str
    parameters: dict[str, Any]

class Ladder(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    yellow: float = -0.20
    halve: float = -0.25
    liquidate: float = -0.30
    recover: float = -0.15
    halved_scale: float = 0.5

class PortfolioVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    product: str
    granularity: str = "ONE_HOUR"
    dataset_id: str
    split_id: str
    members: tuple[Member, ...]
    overlay: Overlay | None
    member_cap: float = 0.15
    band: float = 0.10
    ladder: Ladder = Field(default_factory=Ladder)
    engine_config: EngineConfig                 # capital, costs, min notional; delay 0, no adverse fill
    parent_id: str | None = None
    notes: str = ""

    def version_id(self) -> str:
        return "pv-" + hash_obj(self.model_dump(mode="json"))[:12]
```

  A validator refuses `engine_config.execution_delay_bars != 0`, `adverse_fill`, `rebalance_tolerance != 0` (the
  deadband replaces it), duplicate entry ids and an empty member list. `VersionStore(store)` provides `put`,
  `get`, `list(product=None, state=None)`, `transition(vid, to, actor, evidence=None, note="")`, `events(vid)`
  and the `TRANSITIONS` table as above.
- [ ] **Step 4: PASS. Step 5: Commit** `Portfolio versions and their lifecycle`.

## Task 2: Blending

**Files:** Create `portfolio/blend.py`; Test `tests/test_portfolio.py`.

- [ ] **Step 1: Failing tests** (fixture: synthetic hourly bars as in `tests/conftest.py`, members from the
  `buy_and_hold`, `cash`, `sma_cross` templates)
  - `test_member_shares_use_equal_sleeves_and_the_cap`:
    - four families with 4/3/2/2 members give 0.0625/0.0833/0.125/0.125;
    - two families with 1 member each and a cap of 0.15 give 0.15 each, the excess stays in cash (total 0.30);
    - a probation member gets half its share (the freed half is not redistributed).
  - `test_blend_is_within_zero_one_and_matches_hand_calculation`: two members (always 1, always 0) in one family,
    one member (always 1) in another, no overlay → target 0.5·0.5 + 0.5·1 = 0.75 on every bar.
  - `test_overlay_scales_the_blend`: with an overlay that returns 0.5, the target is 0.375.
  - `test_blend_has_no_lookahead`: the blend at bar i on `bars[:i+1]` equals the full-history blend at i for 12
    evenly spaced i (reuse `strategy/compute.check_positions`).
- [ ] **Step 2: FAIL. Step 3: Implement**
  - `member_shares(version) -> dict[str, float]`. F is the number of families with at least one member. A member's
    share is `min(1/F/n_family, member_cap)`, halved when on probation.
  - `blend(version, bars, paths=None) -> Blend`, a dataclass with:
    - `members: dict[entry_id, np.ndarray]`;
    - `families: dict[family, np.ndarray]` (share-weighted sums);
    - `pre_overlay` (sum over members);
    - `overlay` (ones when there is none);
    - `target = clip(pre_overlay * overlay, 0, 1)`.

    Member targets come from `compute_targets(StrategySpec(...), bars, paths)`; templates only in 3a. A plugin
    member raises `BlendError("plugin members need the sandbox; phase 3b")`. NaN member values (a schedule
    without a decision) are forward-filled, then 0.
- [ ] **Step 4: PASS. Step 5: Commit** `Blend member targets into one target per product`.

## Task 3: Decision step (deadband and ladder)

**Files:** Create `portfolio/decide.py`; Test `tests/test_portfolio.py`.

- [ ] **Step 1: Failing tests**
  - `test_price_drift_alone_never_trades`: last executed target 0.5, blend stays 0.5 while the position's share of
    equity drifts to 0.56 → no order.
  - `test_band_and_full_exit`: from 0.50, a blend of 0.58 → no order, 0.61 → order 0.61; a blend of 0 with a
    position open → order 0 even when the last target was 0.05.
  - `test_first_decision_always_trades`.
  - `test_ladder_halves_recovers_and_liquidates`: equity path 100 → 74.9 (halved: order at blend × 0.5 even
    inside the band) → 85.1 (recovered at −15%, order at the full blend) → 69.9 (liquidated: order 0, halted) →
    every later decision returns no order except 0 while a position remains → `resume()` resets the peak to the
    current equity and the next decision trades the blend.
  - `test_yellow_flag_is_reported_without_action` (state `yellow=True`, no order caused by it).
  - `test_state_round_trips_through_json`.
- [ ] **Step 2: FAIL. Step 3: Implement**

```python
@dataclass
class DeciderState:
    last_target: float | None = None
    peak: float | None = None
    level: Literal["full", "halved", "liquidated"] = "full"
    yellow: bool = False

class Decider:
    def __init__(self, band: float, ladder: Ladder, min_notional: float, state: DeciderState | None = None): ...

    def observe(self, equity: float) -> bool:
        """Update peak and level from a bar-close equity; True when the level changed."""

    def decide(self, blend_target: float, equity: float, position: float) -> float:
        """Order target for this bar, or NaN for no order (A8 deadband, D3 ladder)."""
        changed = self.observe(equity)
        scale = {"full": 1.0, "halved": self.ladder.halved_scale, "liquidated": 0.0}[self.state.level]
        t = float(np.clip(blend_target * scale, 0.0, 1.0))
        full_exit = t == 0.0 and position >= self.min_notional
        if changed or full_exit or self.state.last_target is None or abs(t - self.state.last_target) >= self.band:
            return t
        return math.nan

    def executed(self, target: float) -> None:
        self.state.last_target = target

    def resume(self, equity: float) -> None:
        self.state = DeciderState(last_target=self.state.last_target, peak=equity)
```

  Level rules in `observe` (drawdown `dd = equity / peak - 1`; the peak is not raised while liquidated):
  - full → liquidated if dd ≤ liquidate, else → halved if dd ≤ halve;
  - halved → liquidated if dd ≤ liquidate, → full if dd ≥ recover;
  - liquidated stays until `resume`;
  - yellow = dd ≤ ladder.yellow.
- [ ] **Step 4: PASS. Step 5: Commit** `Portfolio decision step: signal deadband and drawdown ladder`.

## Task 4: Engine decider hook and portfolio backtest

**Files:** Modify `backtest/engine.py`; Create `portfolio/backtest.py`; Test `tests/test_portfolio.py`,
existing `tests/test_backtest*.py`.

- [ ] **Step 1: Failing tests**
  - `test_engine_without_decider_is_unchanged`: the full existing backtest suite passes untouched, and one
    fixed-input equity curve is byte-identical before and after.
  - `test_engine_asks_the_decider_with_the_state_at_the_decision_close`: a recording decider sees, for every
    decided bar, `equity = cash + qty·close` and `position = qty·close` of that bar; `executed` is called only
    when a fill happened.
  - `test_decider_requires_zero_delay`: `execution_delay_bars = 1` with a decider raises `ValueError`.
  - `test_run_portfolio_respects_the_deadband`: a blend oscillating between 0.50 and 0.55 trades once; one
    alternating 0.4/0.6 trades every bar.
  - `test_run_portfolio_logs_every_decision`: the log holds bar, blend, overlay, level, pre-state, order target and
    whether it executed.
- [ ] **Step 2: FAIL. Step 3: Implement**
  - Engine: a new keyword `decider: Decider | None = None`. At fill bar `i` with `decided = i - 1` the call is
    `target = decider.decide(decided, cash + qty * c[decided], qty * c[decided])`. The engine then trades with
    `needs_trade(..., tolerance=0.0, min_notional)` as today and calls `decider.executed(decided, target)` after
    a fill. The docstring gains one execution-contract line.
  - `run_portfolio(version, bars, window, paths=None, initial=None, state=None) -> PortfolioRun(result, blend,
    log)`:
    - compute the `blend` once;
    - wrap `Decider` in an adapter that reads `blend.target[decided]` and appends to the log;
    - call `run_backtest(..., config=version.engine_config, decider=adapter)`.
- [ ] **Step 4: PASS (whole suite). Step 5: Commit** `Engine decider hook; portfolio backtest`.

## Task 5: Dev and validation runs

**Files:** Create `portfolio/evaluate.py`; Modify `experiments/store.py`; Test `tests/test_portfolio.py`.

- [ ] **Step 1: Failing tests** (fixture `paths`, `dataset` from `tests/conftest.py`)
  - `test_dev_run_is_a_trial_and_is_archived`: `run_segment(paths, store, vid, "dev")` creates one experiment with
    kind `portfolio`, `strategy_ref = "portfolio:<vid>"`, `candidate_hash = vid`, segment `dev`, `result_dir`
    with `equity.parquet`, `fills.parquet` and `decisions.parquet`. `trial_count(split)` rises by 1, and the return
    archive has its column (`ReturnArchive.add`).
  - `test_validation_run_is_counted_but_not_a_trial`: kind `portfolio_validation`, `trial_count` unchanged, one
    `validation_uses` row.
  - `test_runs_are_idempotent`: a second dev run of the same version returns the recorded experiment (same
    data hash, window and engine).
  - `test_validation_standard`:
    - `validate(paths, store, vid)` runs dev and validation and moves draft → validated when daily Sharpe ≥ 0.5 and
      max drawdown ≥ −0.25 on validation, else → failed_validation;
    - evidence holds both runs' metrics and the drawdown without the ladder (D4);
    - a second call on a decided version refuses.
- [ ] **Step 2: FAIL. Step 3: Implement**
  - Bars come from `load_research_segment` only, never the holdout file.
  - Metrics:
    - `compute_metrics` from `evaluation/metrics.py` for the usual fields;
    - plus `sharpe_daily` (A4, on `memory.returns.daily_returns`);
    - plus `max_drawdown_no_ladder` (the same run with a ladder whose levels are −∞).
  - Experiment rows go through `store.start_experiment` / `finish_experiment` with the columns `run_experiment`
    fills (dataset, data hash, split, window, engine config id, engine version, metrics).
  - `NON_TRIAL_KINDS` gains `portfolio_validation`.
- [ ] **Step 4: PASS; run `tests/test_memory.py` too. Step 5: Commit** `Portfolio dev and validation runs`.

## Task 6: Seed portfolio definition and the `zqr portfolio` commands

**Files:** Create `portfolio/seed.py`, `portfolio/commands.py`; Modify `cli.py`; Test `tests/test_portfolio.py`.

- [ ] **Step 1: Failing tests**
  - `test_seed_version_takes_every_sleeve_member_and_the_overlay`: with the real `seeds.yaml` imported into a
    fixture library, `seed_version(paths, store, "BTC-USD")` has 11 members (4+3+2+2), the `voltarget-1m`
    overlay, `band` 0.10, the default ladder, and the desk mandate's engine config with
    `rebalance_tolerance = 0`. Shares: tsmom 0.0625 each.
  - `test_seed_is_idempotent`: a second call returns the same version id.
  - `test_portfolio_commands_end_to_end`: `seed`, `validate`, `show`, `list` print JSON; `freeze` without `--note`
    is refused; `freeze` on a draft is refused.
- [ ] **Step 2: FAIL. Step 3: Implement**
  - `seed_version`:
    - library entries of the product with family `sleeve = 1` and kind `member`;
    - the overlay entry of family `vol_overlay`;
    - engine config from the desk mandate of the product (`desk/config.json` `mandates`), with
      `rebalance_tolerance` set to 0;
    - `dataset_id` and `split_id` from that mandate.
  - Commands:
    - `seed --product`;
    - `validate <vid>` (controller);
    - `show <vid>` (body, shares, state, evidence, events);
    - `list`;
    - `freeze <vid> --note` (human);
    - `holdout <vid> [--override-reason]` (Task 7);
    - `approve <vid> --note` (Task 8);
    - `resume <name> --note` (Task 8).
- [ ] **Step 4: PASS. Step 5: Commit** `Seed portfolio definition and zqr portfolio commands`.

## Task 7: Holdout gate for versions (A9, gate 3)

**Files:** Modify `validation/holdout.py`; Test `tests/test_portfolio.py`, `tests/test_experiments.py` (the
holdout-reader test must still pass).

- [ ] **Step 1: Failing tests**
  - `test_portfolio_holdout_needs_a_frozen_version`.
  - `test_portfolio_holdout_records_a_viewing_and_applies_gate_3`:
    - one `holdout_runs` row with `candidate_hash = vid`;
    - kind `holdout`;
    - state `final_evaluated` when daily Sharpe ≥ 0.5 and total return > 0, else `failed_final`;
    - evidence has the metrics and the prior viewings.
  - `test_one_version_per_split_per_seven_days`: a second frozen version on the same split within 7 days is
    refused; after 7 days (injected clock) it runs.
  - `test_portfolio_holdout_respects_the_split_cap_and_override`: at the cap it is refused; with
    `override_reason` it runs and the split is marked contaminated, as for candidates.
  - `test_a_repeat_does_not_spend_a_viewing`: the same version again returns the recorded result.
- [ ] **Step 2: FAIL. Step 3: Implement** `run_portfolio_holdout(paths, store, version_id, override_reason=None,
  now=time.time)` next to `run_holdout`, sharing `split_viewing_cap`, `load_holdout_data`,
  `record_holdout_run` and `record_holdout_override`. The bars are the full frozen dataset (`full.parquet`) with the
  holdout window, run through `portfolio.backtest.run_portfolio`. The experiment row and result files are as in
  Task 5. The 7-day rule counts earlier `holdout_runs` on the split whose `candidate_hash` starts with `pv-`.
- [ ] **Step 4: PASS. Step 5: Commit** `Holdout gate for portfolio versions`.

## Task 8: Paper portfolio trader

**Files:** Modify `paper/trader.py`, `paper/fleet.py`; Create `paper/portfolio_trader.py`; Test
`tests/test_paper.py`, `tests/test_fleet.py`.

- [ ] **Step 1: Failing tests**
  - `test_trader_refactor_changes_nothing`: the whole existing paper and fleet suite passes after extracting
    `_target` and `_order_gate`.
  - `test_portfolio_replay_matches_run_portfolio`: a version of two template members replayed through the paper
    stack (`run_replay` with a `PortfolioTrader`) gives the same order targets, sides and quantities (within a
    cent) as `run_portfolio` on the same bars, under the same fill model.
  - `test_decider_state_survives_a_restart`: stop after bar 150 and restart → the same orders as an uninterrupted
    run (state in the ledger key `portfolio_state`).
  - `test_catch_up_bars_update_the_ladder_without_trading`: `ingest` calls `observe`, never orders.
  - `test_liquidated_portfolio_waits_for_resume`: after −30%, decisions record `halted` and no buys;
    `zqr portfolio resume` writes a resume request that the trader applies on its next bar (peak reset, logged).
  - `test_fleet_attaches_a_portfolio_entry`:
    - a roster entry with `source "portfolio"`, `portfolio_version = vid` and
      `strategy_ref = "portfolio:<vid>"` runs a `PortfolioTrader`;
    - `max_order_notional` = `initial_cash` (D1);
    - changing `portfolio_version` is refused like any identity change;
    - the paper slot cap ignores it.
  - `test_approve_adds_the_roster_entry`: `zqr portfolio approve <vid>` on `final_evaluated` →
    - state `paper`;
    - entry `portfolio-btc-usd`;
    - library entries of the members move `candidate → member` (actor human, note with the version id);
    - refused for any other state.
- [ ] **Step 2: FAIL. Step 3: Implement**
  - `PaperTrader`: move the target computation of `on_bar` into `_target(bar, avail) -> TargetPortfolio | None`
    and the `needs_trade` call of `_size` into `_order_gate(target, current_value, equity) -> bool`.
  - `PortfolioTrader` overrides:
    - `_target`: computes `blend` on `self.history` and `decide` from the stored state; the order target goes
      into `TargetPortfolio` with `candidate_hash = vid`, or None for no order;
    - `_order_gate`: `needs_trade(tolerance=0.0)`;
    - `_send`: calls `executed` and saves the state;
    - `ingest`: observes;
    - the decision record gains `"portfolio": {blend, pre_overlay, overlay, level, yellow, peak, pre_state,
      order_target, family_targets}`.
  - The resume request is the file `paper/<name>/resume.json` (`note`, `at`), consumed once and logged as an
    ALERT event.
  - `RosterEntry` gains `portfolio_version: str | None` and `source` gains `"portfolio"`. `_config` loads the
    version body from `VersionStore` into `PaperConfig.portfolio` (part of the identity hash). `make_trader` picks
    the class from `cfg.portfolio`.
- [ ] **Step 4: PASS. Step 5: Commit** `Paper portfolio trader`.

## Task 9: Portfolio parity

**Files:** Modify `paper/parity.py`; Test `tests/test_paper.py`.

- [ ] **Step 1: Failing tests**
  - `test_portfolio_parity_is_clean_on_a_replay`: after the Task 8 replay, every check passes and the path gap is
    under 1 bp.
  - `test_portfolio_parity_finds_a_changed_band`: a ledger written with band 0.05 and checked against a version
    with band 0.10 reports order mismatches.
  - `test_portfolio_parity_finds_a_wrong_blend`: a tampered recorded blend is a target mismatch.
- [ ] **Step 2: FAIL. Step 3: Implement** `portfolio_parity(entry, roster, ledger, bars, version, paths, since,
  until)`. For each checkable decision (the same `history_start` rule as `paper_parity`):
  - recompute the blend from the bar store and compare it with the recorded `portfolio.blend`;
  - re-run `Decider.decide` from the recorded `pre_state` with the marked equity and position, and compare it with
    the recorded order target;
  - check side and size with the existing `_size_check` (order cap, cash buffer and risk limits stay allowed
    differences);
  - path: `run_portfolio` from the first decision's state (`initial` and `state`).

  The returned dict has the same keys as `paper_parity`, so the report code is shared.
- [ ] **Step 4: PASS. Step 5: Commit** `Portfolio parity`.

## Task 10: Report: portfolios, combined account, monthly review

**Files:** Modify `desk/report.py`, `desk/commands.py`; Test `tests/test_report.py`.

- [ ] **Step 1: Failing tests**
  - `test_report_has_a_portfolio_section`. Per product portfolio:
    - equity, day and total return, and buy and hold with the same capital and costs;
    - drawdown from the peak, ladder level, yellow flag;
    - composition (family targets and order target at the last decision);
    - fills, turnover and fees in the day;
    - parity from `portfolio_parity`;
    - the version and its state.
  - `test_combined_account_is_the_sum_of_the_two_portfolios`: combined equity = BTC + ETH; benchmark = $10,000 per
    product bought at each portfolio's first decision price with entry costs; the combined drawdown is shown with
    the yellow flag only.
  - `test_family_contribution_splits_daily_pnl_by_target_share`: each day's P&L is split in proportion to the
    families' shares of the order target at the start of the day; the shares sum to the day's P&L.
  - `test_research_section_shows_versions_and_validation_uses`: per split, the portfolio versions by state,
    validation uses and holdout viewings left.
  - `test_monthly_report_on_the_first`: `write_monthly_report(paths, store, "2026-11")` writes
    `desk/reports/monthly-2026-11.md` with the month's return per portfolio and combined against buy and hold,
    drawdowns, ladder events, family contribution, versions, and the soft-stop status (Task 11). The daily report
    job writes it on the first UTC day of a month for the month just ended.
- [ ] **Step 2: FAIL. Step 3: Implement** (read-only: ledgers, marks, decision records, `VersionStore`).
  Portfolio entries are left out of the control table and get their own section.
- [ ] **Step 4: PASS. Step 5: Commit** `Report: portfolios, combined account and monthly review`.

## Task 11: Soft stop (D2)

**Files:** Create `desk/softstop.py`; Modify `desk/research.py`, `desk/commands.py`; Test `tests/test_desk.py`.

- [ ] **Step 1: Failing tests**
  - `test_soft_stop_waits_for_twelve_months`: with 11 months of combined marks → `{"status": "too early"}`.
  - `test_soft_stop_triggers_below_minus_half`:
    - 12 months where the combined Sharpe is 0.6 below the benchmark's → `soft_stop.json` written with both Sharpe
      ratios and the window;
    - the research loop returns `paused` (`soft stop: human review required`) even when `research_enabled` is
      true;
    - at 0.4 below, nothing happens.
  - `test_clear_needs_a_note_and_is_logged`.
- [ ] **Step 2: FAIL. Step 3: Implement**
  - `evaluate(paths, day) -> dict` runs on monthly review days from the monthly report job.
  - The Sharpe ratios come from the daily combined and benchmark returns over the trailing 365 days (A4 formula).
  - `clear(paths, note)` moves the file to `desk/soft_stop.cleared-<ts>.json` and logs the note.
  - In the research loop the soft-stop check comes right after the `research_enabled` check.
- [ ] **Step 4: PASS. Step 5: Commit** `Soft stop rule (investment policy 2)`.

## Task 12: Docs and full verification

- [ ] `docs/development.md`:
  - a new section 5.9 Portfolio layer: version, blend, decider, backtest hook, runs, holdout, paper trader,
    parity, reports, soft stop;
  - commands table rows for `zqr portfolio …` and `zqr desk soft-stop …`;
  - the `ZQR_HOME` layout (`paper/portfolio-*`, `desk/soft_stop.json`, monthly reports);
  - M-table status and the test count.
- [ ] Full suite outside the sandbox: `MPLCONFIGDIR=/tmp/mpl .venv/bin/pytest -q -p no:warnings`;
  `.venv/bin/ruff check src tests`.
- [ ] Local rehearsal on a copy of production `experiments.db` and `library/` (read-only source, as in phase 2):
  `zqr portfolio seed` for both products, then `zqr portfolio validate`. Compare with the seed characterisation
  runs, which should be the same order of magnitude: the blend's dev Sharpe sits between its members', and turnover
  is below the most active member's.
- [ ] Commit.

## Task 13: Release, production run, checkpoint

- [ ] Ask the user before merging, pushing and releasing.
- [ ] After release: `zqr backup`, then on WSL:
  - `zqr portfolio seed --product BTC-USD`;
  - `zqr portfolio seed --product ETH-USD`;
  - `zqr portfolio validate <vid>` for each.
- [ ] **Checkpoint (design 7.1).** Send the human:
  - per product: the dev and validation results (daily Sharpe, max drawdown with and without the ladder, total
    return, turnover, costs, time at each ladder level);
  - the validation standard's verdict;
  - the member and family contributions;
  - the comparison with buy and hold.

  No freeze or holdout happens before their decision. If a portfolio fails validation, it is recorded and
  discussed, not adjusted and retried (A6).
- [ ] After approval (separate step, human commands): `zqr portfolio freeze`, `zqr portfolio holdout`,
  `zqr portfolio approve`, then watch the first day of the portfolio traders in the report (parity, fills,
  ladder state).

## Order and dependencies

Task 0 comes first. Tasks 1-3 are independent; 4 needs 2 and 3; 5 needs 1 and 4; 6 needs 5; 7 needs 5 and 6; 8 needs
1-4; 9 needs 8; 10 needs 8 and 9; 11 needs 10. Tasks 12-13 come last.

## Phase 3b outline (separate plan, after the seed portfolio runs in paper)

- **Registration (A6):** an immutable `hypotheses` table (prior text, citations, 1-3 variants with code hash and
  parameters). Human registration in 3b; campaign registration in phase 4.
- **Gates 1-2:**
  - marginal value with `split_stats.appraisal_test` against the current paper version's dev returns;
  - do no harm on validation (Sharpe not lower, drawdown at most 2 points worse, turnover at most 25% higher);
  - the agent sees verdicts only.
- **Version switching on the same paper account:** a new version replaces the rule of `portfolio-<product>`
  without resetting cash, position or the drawdown peak, at most once per 7 days (A9). This needs a fleet path
  that accepts a version change for portfolio entries only, after `approve`.
- **Probation (A10):** half share for 90 days; promotion or demotion on decision parity and the residual test
  `α / se(α) < −1` on shadow returns.
- **Retirement proposals (design 5.7):** a periodic reverse test; the human confirms.
- **Plugin members:** blend through the sandbox (Docker), as parity does for plugin controls.

## Risks

- **The seed portfolio may fail validation.** Validation is 1.16 years with a Sharpe standard error near 0.93, and
  the members are trend rules on one coin. A failure is a result, not a bug. The rule forbids tuning it into a pass.
- **Trend concentration (R6).** All four sleeve families are trend following and share one large cluster on BTC
  (A13.2). The sleeves diversify implementation, not the source of return. The report's family contribution makes
  this visible; finding an uncorrelated family is phase 4's first campaign theme.
- **Compute in paper:** the trader blends 12 templates on the full bar store every hour per product. The rolling
  windows are vectorised, so this takes about a second. If it grows, cache member targets by bar.
- **Dev warm-up:** the first year of dev is partly invested (see Facts), which lowers the dev Sharpe. Validation
  and holdout are unaffected.
- **The fee tier is unverified until the human checks it.** A higher real fee changes every threshold's meaning
  and requires new mandate versions before Task 6.
- **The paper fleet keeps running;** the release restarts the container for a few seconds, as before. Research
  stays disabled.
