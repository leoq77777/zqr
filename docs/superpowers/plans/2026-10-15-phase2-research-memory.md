# Phase 2: research memory, clustering and statistics — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the 1,788 existing dev trials and the 182 session reports into research memory: a daily return
archive, residual clusters with the effective number of trials K, the split DSR and the registered-test statistic,
family tags and deterministic family archives, and a clustering report for the human checkpoint.

**Rules:** `docs/contracts-v2-appendix-a.md` sections A1-A5 and A7 (must be approved before Task 1; A12 points 1-4
change the code). Design: `docs/superpowers/specs/2026-10-13-research-library-portfolio-design.md` section 4.

**Architecture:** A new package `quant_research/memory/` with one module per step (returns, residuals, clusters,
families, conclusions, archives, report), backed by new tables in `experiments.db` and parquet files under
`ZQR_HOME/library/`. Statistics go next to the existing ones in `evaluation/`. A `zqr memory` command group runs
each step; every step is deterministic and idempotent. Nothing in this phase gates research or trading: research
stays disabled, the paper fleet is untouched.

**Tech stack:** Python 3.12, numpy, pandas, SQLite, pyarrow; new dependency `scipy` (hierarchical clustering only).

**Branch:** `phase2` (already holds the unreleased debt fixes `e18c341`, `cfd7d05`).

---

## Facts this plan relies on (checked on production, 2026-10-15)

- Splits: BTC `split-1016c7c9873a` (dataset `coinbase-btc-usd-one_hour-0d7202d6b155`), ETH `split-c47a0be57a24`.
  Dev window for both: 1609459200-1718906400 (2021-01-01 00:00 to 2024-06-20 18:00 UTC), 30,402 hourly bars.
- Trials (all kind `backtest`, segment `dev`, one engine version `1.1.0`, one policy `policy-fddbb690001b`):
  BTC 879 completed, 4 failed, 1 rejected (3 blocked, not trials); ETH 899 completed, 1 failed, 1 rejected.
- Each completed experiment has `results/<exp_id>/equity.parquet` with columns
  `time, cash, qty, close, equity, exposure` (`time` = bar open, equity marked at that bar's close).
- 184 sessions, 182 with `sessions/<sid>/research.json` (fields `research_id, task_id, hypothesis,
  preregistered_plan, experiment_ids, strategy_artifacts, results, decision, limitations, sources, unresolved,
  final_artifact`). Topic and product per session: `desk/research_ledger.jsonl` (183 rows; topics `trend`,
  `mean_reversion`, `volatility_filter`, `multi_timeframe`, 23 sessions each per product, ETH multi_timeframe 22).
- `run_experiment(..., gate=GateContext(caller="controller"))` runs on dev without a session.
- The gateway's citation check is `ResearchGateway.build_report` (`gateway/service.py:303`), bound to a live session.

## File structure

| File | Responsibility |
|---|---|
| Create `src/quant_research/evaluation/split_stats.py` | A4 formulas: `sharpe_daily`, `sigma0`, `sigma_sr`, `sr0`, `dsr_k`, `appraisal_test` |
| Create `src/quant_research/memory/__init__.py` | Package docstring |
| Create `src/quant_research/memory/returns.py` | A1: `daily_returns(equity, initial_cash)`, `ReturnArchive` (parquet per split + `return_index` table), `backfill` |
| Create `src/quant_research/memory/residuals.py` | A2: market returns from the segment bars, OLS residuals, winsorising, flat/passive flags |
| Create `src/quant_research/memory/clusters.py` | A3: correlation, average linkage, cuts, canonical labels, K; tables `clusterings`, `cluster_members` |
| Create `src/quant_research/memory/families.py` | Family tags (legacy topics, seeds), cluster-to-family map at 0.3, new-family proposals |
| Create `src/quant_research/memory/characterise.py` | A7: run the 24 seed instances on dev as kind `seed` |
| Create `src/quant_research/memory/conclusions.py` | Append-only `family_conclusions`; import of the 182 session reports with the citation check |
| Create `src/quant_research/memory/archive.py` | Deterministic family archives (`library/archives/<family>.json` and `.md`) |
| Create `src/quant_research/memory/report.py` | Clustering report for the checkpoint (`library/reports/clustering-<date>.md`) |
| Create `src/quant_research/memory/commands.py` | `zqr memory backfill / characterise / cluster / import-conclusions / archive / report / status` |
| Modify `src/quant_research/gateway/service.py` | Extract the citation check into `citation_problems(store, session_id, doc, workspace)`; `build_report` calls it |
| Modify `src/quant_research/experiments/runner.py` | After persisting a completed dev trial, append it to the return archive |
| Modify `src/quant_research/experiments/store.py` | Nothing structural; `seed` is a trial kind by default (not in `NON_TRIAL_KINDS`) |
| Modify `src/quant_research/desk/report.py` | Research section: K at three thresholds and the earliest next-split date per split |
| Modify `src/quant_research/library/commands.py` | `zqr library show`: dev statistics and split DSR of the instance's seed run |
| Modify `src/quant_research/cli.py` | Register `memory` commands |
| Modify `pyproject.toml`, `uv.lock` | Add `scipy` |
| Tests | `tests/test_split_stats.py`, `tests/test_memory.py` (returns, residuals, clusters, families, conclusions, archives), additions to `tests/test_report.py`, `tests/test_library.py` |
| Docs | `docs/development.md` (new section 5.8 Research memory, layout, commands, test count) |

Storage layout under `ZQR_HOME/library/`: `returns/<split_id>.parquet`, `archives/<family_id>.{json,md}`,
`reports/clustering-<YYYY-MM-DD>.md`.

New tables in `experiments.db` (created by `memory.schema.ensure(store)` with `CREATE TABLE IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS return_index (
    experiment_id TEXT PRIMARY KEY, split_id TEXT NOT NULL, kind TEXT, session_id TEXT, family_tag TEXT,
    tag_source TEXT, n_days INTEGER, sharpe REAL, turnover REAL, no_returns INTEGER NOT NULL, flat INTEGER,
    passive INTEGER, added_at REAL);
CREATE TABLE IF NOT EXISTS clusterings (
    id TEXT PRIMARY KEY, split_id TEXT NOT NULL, computed_at REAL, input_hash TEXT, n_trials INTEGER,
    k TEXT, summary TEXT);                      -- k: {"0.3": .., "0.5": .., "0.7": ..}
CREATE TABLE IF NOT EXISTS cluster_members (
    clustering_id TEXT, threshold REAL, experiment_id TEXT, label TEXT,
    PRIMARY KEY (clustering_id, threshold, experiment_id));
CREATE TABLE IF NOT EXISTS family_proposals (
    id TEXT PRIMARY KEY, clustering_id TEXT, split_id TEXT, label TEXT, body TEXT, status TEXT, decided_at REAL);
CREATE TABLE IF NOT EXISTS family_conclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, family_id TEXT, product TEXT, source TEXT, session_id TEXT UNIQUE,
    body TEXT NOT NULL, citations_ok INTEGER, problems TEXT, created_at REAL);
```

`family_conclusions` is append-only: no UPDATE or DELETE statement exists for it; the `UNIQUE(session_id)`
constraint makes the import idempotent. Human notes use `source = 'human'` with `session_id` NULL.

---

## Task 1: Split statistics (A4)

**Files:** Create `src/quant_research/evaluation/split_stats.py`; Test `tests/test_split_stats.py`.

- [ ] **Step 1: Write the failing tests**

```python
import math
import numpy as np
import pytest
from quant_research.evaluation import split_stats as ss

T = 1267

def test_sigma0_and_sr0_reference_values():
    assert ss.sigma0(T) == pytest.approx(0.5369, abs=1e-4)
    expected = {1: 0.0, 2: 0.279, 3: 0.458, 20: 1.021, 110: 1.377, 260: 1.530}
    for k, v in expected.items():
        assert ss.sr0(k, T) == pytest.approx(v, abs=1e-3)

def test_needed_sharpe_for_dsr_0_9_matches_appendix_table():
    needed = {1: 0.688, 3: 1.146, 20: 1.709, 260: 2.219}
    for k, v in needed.items():
        assert ss.sr0(k, T) + 1.2815515655 * ss.sigma0(T) == pytest.approx(v, abs=1e-3)

def test_sigma_is_floored_at_the_null_on_a_skewed_series():
    r = np.full(T, -0.0002); r[100] = 0.9          # one day dominates: skewness ~ 35
    s = ss.sharpe_daily(r)
    assert ss.sigma_sr(r) < ss.sigma0(T)            # the fat-tail formula would be lenient
    assert ss.dsr_k(r, 1) == pytest.approx(ss.norm_cdf(s / ss.sigma0(T)))

def test_flat_series_has_zero_sharpe_and_dsr_one_half_at_k1():
    r = np.zeros(T)
    assert ss.sharpe_daily(r) == 0.0 and ss.dsr_k(r, 1) == 0.5

def test_appraisal_test_passes_an_uncorrelated_modest_variant_and_fails_a_redundant_strong_one():
    rng = np.random.default_rng(0)
    p = rng.normal(0.004, 0.03, T)                        # portfolio, realised SR 1.80
    modest = rng.normal(0.0016, 0.02, T)                  # independent, SR 1.09, AR 1.17, statistic 0.985
    redundant = 1.2 * p + rng.normal(0.0, 0.002, T)       # SR 1.80 but a copy: AR 0.05, statistic 0.54
    assert ss.appraisal_test(modest, p, k=1)["statistic"] >= 0.9
    assert ss.appraisal_test(redundant, p, k=1)["statistic"] < 0.9

def test_appraisal_test_refuses_more_than_three_variants():
    with pytest.raises(ValueError):
        ss.appraisal_test(np.zeros(T), np.zeros(T), k=4)
```

- [ ] **Step 2: Run them, expect ImportError**

Run: `.venv/bin/pytest tests/test_split_stats.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement**

```python
"""Statistics on daily returns for portfolio research (contracts v2 appendix A, section A4).

The luck dispersion is the null's (`sigma0`), not the spread of trial Sharpe ratios; the denominator is floored
at the null so fat tails never make a test more lenient.
"""
from __future__ import annotations
import math
from statistics import NormalDist
import numpy as np

DAYS = 365
EULER_GAMMA = 0.5772156649015329
MAX_REGISTERED = 3
_N = NormalDist()
norm_cdf = _N.cdf

def sharpe_daily(r) -> float:
    r = np.asarray(r, float); sd = r.std(ddof=1) if len(r) > 1 else 0.0
    return 0.0 if sd == 0 else float(r.mean() / sd * math.sqrt(DAYS))

def _moments(r: np.ndarray) -> tuple[float, float]:
    sd0 = r.std(ddof=0)
    if sd0 == 0:
        return 0.0, 3.0
    z = (r - r.mean()) / sd0
    return float((z**3).mean()), float((z**4).mean())

def sigma0(t: int) -> float:
    return math.sqrt(DAYS / (t - 1))

def sigma_sr(r) -> float:
    r = np.asarray(r, float); s = sharpe_daily(r) / math.sqrt(DAYS); g3, g4 = _moments(r)
    term = 1 - g3 * s + (g4 - 1) / 4 * s * s
    return sigma0(len(r)) if term <= 0 else math.sqrt(term / (len(r) - 1)) * math.sqrt(DAYS)

def sr0(k: int, t: int) -> float:
    if k <= 1:
        return 0.0
    return sigma0(t) * ((1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / k) + EULER_GAMMA * _N.inv_cdf(1 - 1 / (k * math.e)))

def dsr_k(r, k: int) -> float:
    r = np.asarray(r, float)
    return norm_cdf((sharpe_daily(r) - sr0(k, len(r))) / max(sigma0(len(r)), sigma_sr(r)))

def appraisal_test(candidate, portfolio, k: int) -> dict[str, float]:
    if not 1 <= k <= MAX_REGISTERED:
        raise ValueError(f"a registered hypothesis has 1 to {MAX_REGISTERED} variants, not {k}")
    c, p = np.asarray(candidate, float), np.asarray(portfolio, float)
    beta = float(np.cov(c, p, ddof=1)[0, 1] / p.var(ddof=1)) if p.var() > 0 else 0.0
    h = c - beta * p
    ar = sharpe_daily(h)
    return {"beta": beta, "alpha_daily": float(h.mean()), "appraisal_ratio": ar, "k": k,
            "statistic": dsr_k(h, k)}
```

- [ ] **Step 4: Run the tests, expect PASS.** The commented values were computed with these seeds on 2026-10-15
  (the skewed series: skewness 35.6, `σ_SR` 0.344 < `σ0` 0.537). Never loosen the 0.9 standard to make a test pass.
- [ ] **Step 5: Commit** `git commit -m "Split statistics on daily returns (appendix A4)"`.

## Task 2: Daily returns (A1)

**Files:** Create `src/quant_research/memory/__init__.py`, `src/quant_research/memory/returns.py`;
Test `tests/test_memory.py`.

- [ ] **Step 1: Failing tests**
  - `test_daily_returns_use_the_last_bar_of_each_utc_day`: 50 hourly rows starting 2021-01-01 00:00 with equity
    rising 1 per bar from 10,001; expect 3 days (24, 24, 2 bars), `r0 = 10024/10000 - 1`,
    `r1 = 10048/10024 - 1`, `r2 = 10050/10048 - 1`, index = the three dates.
  - `test_daily_returns_of_a_real_dev_window_have_1267_days`: synthetic 30,402 rows from 1609459200 → 1,267 days,
    last day has 18 bars.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `daily_returns(equity: pd.DataFrame, initial_cash: float) -> pd.Series`:
  group by `time.dt.tz_convert("UTC").dt.date`, take the last `equity` per day, prepend `initial_cash`,
  `pct_change().iloc[1:]`, index named `day`.
- [ ] **Step 4: PASS. Step 5: Commit.**

## Task 3: Return archive and backfill (A1)

**Files:** Modify `memory/returns.py`; Create `memory/schema.py` (the SQL above, `ensure(store)`);
Test `tests/test_memory.py`.

- [ ] **Step 1: Failing tests** (fixture: a temporary `ZQR_HOME` with a small frozen dataset and split, as in
  `tests/test_experiments.py`; run 3 template backtests through `run_experiment`, plus one forced failure and one
  `blocked` request):
  - `test_backfill_archives_every_completed_dev_trial_once`: after `backfill(paths, store)`, the parquet has the 3
    completed experiment columns; `return_index` has 4 rows (3 + the failure with `no_returns = 1`); the blocked
    request is absent; running `backfill` again changes nothing (same file hash, same rows).
  - `test_backfill_skips_non_trial_kinds`: a `sensitivity` experiment on the same split is not archived.
  - `test_archive_columns_are_sorted_by_experiment_id` (determinism of the parquet).
- [ ] **Step 2: FAIL. Step 3: Implement**
  - `ReturnArchive(paths, store)` with `path(split_id)`, `load(split_id) -> pd.DataFrame`, `add(exp_row)`,
    `index(split_id)`. Writes go to a temporary file then `os.replace`. Columns sorted by experiment id.
  - Trial selection reuses the store's rule: status not `blocked`, kind not in `NON_TRIAL_KINDS`,
    `segment = 'dev'`. Completed rows read `result_dir/equity.parquet` and `engine_config.initial_cash`.
  - `sharpe` = `split_stats.sharpe_daily`, `turnover` from `metrics.turnover`.
  - `backfill(paths, store, split_id=None) -> dict` returns counts `{split: {"added", "no_returns", "present"}}`.
- [ ] **Step 4: PASS. Step 5: Commit.**

## Task 4: Archive new trials as they finish

**Files:** Modify `src/quant_research/experiments/runner.py` (`_persist`, `_fail`); Test `tests/test_memory.py`.

- [ ] **Step 1: Failing test** `test_a_new_dev_trial_is_archived_when_it_completes`: run one experiment; its column
  is in the archive without calling `backfill`. A holdout experiment is not archived.
- [ ] **Step 2: FAIL. Step 3: Implement**: at the end of `_persist` (completed) and `_fail` (failed/rejected), call
  `ReturnArchive(paths, store).add(row)` when the row is a dev trial; archive errors are logged in the experiment's
  `notes` and never fail the experiment.
- [ ] **Step 4: PASS; also run `tests/test_experiments.py` and `tests/test_gateway.py`. Step 5: Commit.**

## Task 5: Residuals, flat and passive series (A2)

**Files:** Create `memory/residuals.py`; Test `tests/test_memory.py`.

- [ ] **Step 1: Failing tests**
  - `test_market_returns_are_close_to_close_on_the_day_grid` (first day over the first open).
  - `test_residuals_remove_the_market_component`: `r = 0.0002 + 0.6·m + noise` → `b ≈ 0.6`, residual correlation
    with `m` ≈ 0.
  - `test_winsorising_clips_at_three_standard_deviations_once`.
  - `test_flat_and_passive_series_are_flagged`: zeros → flat; `r = m` (pure buy and hold) → passive; a real trend
    rule → neither.
- [ ] **Step 2: FAIL. Step 3: Implement** `market_returns(bars, window) -> pd.Series`,
  `residuals(returns: pd.DataFrame, market: pd.Series) -> (winsorised: pd.DataFrame, flags: pd.DataFrame)`.
  Thresholds as module constants `FLAT_ABS = 1e-12`, `PASSIVE_SD = 1e-4`, `WINSOR_SD = 3.0`.
- [ ] **Step 4: PASS. Step 5: Commit.**

## Task 6: Clustering and K (A3)

**Files:** Create `memory/clusters.py`; Modify `pyproject.toml`, `uv.lock` (`uv add scipy`, run outside the
sandbox); Test `tests/test_memory.py`.

- [ ] **Step 1: Failing tests**
  - `test_planted_clusters_are_recovered`: three latent factors, 10 series each with residual correlation ≈ 0.8
    within and ≈ 0 across, plus 5 independent series → at ρ* = 0.7: 3 + 5 clusters; at 0.3 the same; K adds flat,
    passive and no-return trials exactly as A3 says.
  - `test_labels_are_canonical_and_recomputation_is_identical`: shuffle the input columns → same labels (smallest
    experiment id per cluster) and same `input_hash`.
  - `test_k_counts_flat_passive_and_trials_without_returns_once_each`.
- [ ] **Step 2: FAIL. Step 3: Implement** `cluster(winsorised, flags, no_returns: list[str]) -> Clustering`
  (`scipy.cluster.hierarchy.linkage(squareform(dist, checks=False), "average")`,
  `fcluster(Z, t=sqrt((1-ρ*)/2), criterion="distance")`), `THRESHOLDS = (0.3, 0.5, 0.7)`, `K_THRESHOLD = 0.7`;
  `record(store, split_id, clustering)` writes `clusterings` and `cluster_members`;
  `latest(store, split_id)` reads back; `effective_k(store, split_id) -> int`.
  `input_hash` = sha256 over sorted experiment ids and the archive's parquet bytes.
- [ ] **Step 4: PASS. Step 5: Commit** (including the lock file).

## Task 7: Family tags and cluster mapping (A12 point 4)

**Files:** Create `memory/families.py`; Test `tests/test_memory.py`.

- [ ] **Step 1: Failing tests**
  - `test_legacy_topics_map_to_tags`: `volatility_filter` → `vol_overlay`; `trend`, `multi_timeframe` → None;
    `mean_reversion` → `short_term_reversal` (`tag_source = "legacy_topic"`).
  - `test_a_cluster_with_a_seed_takes_the_seed_family`; with seeds of two families, the majority wins and ties go to
    the family of the cluster's lowest-id seed.
  - `test_a_cluster_with_no_seed_and_no_tag_majority_becomes_one_proposal`; recomputing does not duplicate it.
- [ ] **Step 2: FAIL. Step 3: Implement**
  - `LEGACY_TOPICS = {"volatility_filter": "vol_overlay", "mean_reversion": "short_term_reversal",
    "trend": None, "multi_timeframe": None}`; session → topic from `desk/research_ledger.jsonl`.
  - `tag_trials(paths, store)` fills `return_index.family_tag/tag_source` (seeds: their entry's family, source
    `seed`).
  - `map_clusters(store, split_id, threshold=0.3)` → `{label: family_id | None}`; unmapped clusters with at least
    5 trials create `family_proposals` rows (member count, parameter summary from the experiments, representative
    = highest dev Sharpe, statistics). Smaller unmapped clusters are listed in the report, not proposed.
  - `short_term_reversal` is added to the library as a family with `sleeve: false` and an archive note only after
    the human approves A12 point 4 (it is a catalog change: new `seeds.yaml` version, imported with
    `zqr library import`).
- [ ] **Step 4: PASS. Step 5: Commit.**

## Task 8: Seed characterisation runs (A7)

**Files:** Create `memory/characterise.py`; Test `tests/test_memory.py`.

- [ ] **Step 1: Failing test** `test_characterise_runs_each_seed_instance_once_on_dev`: with a test catalog of two
  entries on a fixture split, `characterise(paths, store, catalog)` creates two kind-`seed` experiments tagged
  with their entry id (`notes = {"library_entry": ...}`); a second call creates none; both are counted by
  `trial_count` and archived.
- [ ] **Step 2: FAIL. Step 3: Implement**: for each `catalog.instances()` whose product matches a split's
  dataset, build `ExperimentRequest(strategy_ref=f"template:{entry.template}", parameters=..., segment="dev",
  engine_config=<the mandate's engine config>, policy=PortfolioPolicy())` and call
  `run_experiment(..., kind="seed", gate=GateContext(caller="controller"))`; skip instances whose entry id already
  has a `seed` experiment on the split. Engine config: the desk mandate's (fee 0.6%, 5 bps, $10,000), read from
  `desk/config.json` via `load_config`.
- [ ] **Step 4: PASS. Step 5: Commit.**

## Task 9: Session conclusions import

**Files:** Modify `gateway/service.py`; Create `memory/conclusions.py`; Test `tests/test_memory.py`,
`tests/test_gateway.py` (unchanged behaviour).

- [ ] **Step 1: Failing tests**
  - `test_citation_problems_matches_build_report`: the extracted function returns the same problems as
    `build_report` on the existing gateway fixtures (valid report, unknown id, wrong cited number).
  - `test_import_conclusions_is_append_only_and_idempotent`: two fixture sessions → two rows with family from the
    topic map, product from the ledger, `citations_ok` from the check; a second import adds nothing; a modified
    `research.json` on disk does not change the stored row.
  - `test_a_report_with_wrong_numbers_is_imported_with_its_problems` (kept as evidence, `citations_ok = 0`).
- [ ] **Step 2: FAIL. Step 3: Implement**
  - `citation_problems(store, session_id, doc, workspace: Path) -> list[str]` holds the body of
    `build_report` minus the file reading and event logging; `build_report` calls it.
  - `import_conclusions(paths, store) -> dict`: for each `sessions/<sid>/research.json`, body =
    `{hypothesis, preregistered_plan, decision, limitations, unresolved, experiment_ids, results}` plus the
    session's topic, product, run id; `INSERT OR IGNORE`.
- [ ] **Step 4: PASS. Step 5: Commit.**

## Task 10: Deterministic family archives

**Files:** Create `memory/archive.py`; Test `tests/test_memory.py`.

- [ ] **Step 1: Failing tests**
  - `test_archive_is_byte_identical_on_regeneration`.
  - `test_archive_lists_members_trials_clusters_and_conclusions` for a fixture family with two seeds, three legacy
    trials and one conclusion.
  - `test_cost_sensitivity_uses_non_trial_reruns`: the representative's 2× cost Sharpe comes from a `sensitivity`
    experiment (not a trial: `trial_count` unchanged).
- [ ] **Step 2: FAIL. Step 3: Implement** `build_archive(paths, store, family_id) -> dict` and `render_md(dict)`.
  Contents (design 4.6), per product:
  - members and representative (highest dev Sharpe among seeds, else among tagged trials) with dev Sharpe, max
    drawdown, turnover, split DSR (A5);
  - parameter ranges tried (per `strategy_ref`, min/max of each numeric parameter), trials, clusters at 0.3/0.7;
  - cost sensitivity: Sharpe at 1× and 2× costs for the representative (a `sensitivity` rerun with doubled fee and
    slippage, created once);
  - regimes: dev days split by the sign of the coin's trailing 30-day return (trend up / down) and by trailing
    30-day volatility above or below its dev median; representative's annualised Sharpe in each;
  - correlation of the representative's residuals with the other families' representatives;
  - failure classes: counts of diagnosis labels across the family's trials; holdout viewings used on the split;
  - conclusions: the `family_conclusions` rows, oldest first.
  Files are written with sorted keys and fixed float formatting; no timestamps inside the deterministic part.
- [ ] **Step 4: PASS. Step 5: Commit.**

## Task 11: Clustering report and `zqr memory` commands

**Files:** Create `memory/report.py`, `memory/commands.py`; Modify `cli.py`; Test `tests/test_memory.py`.

- [ ] **Step 1: Failing tests**
  - `test_memory_commands_run_end_to_end_on_a_fixture_home`: `backfill`, `characterise`, `cluster`,
    `import-conclusions`, `archive`, `report` in order; each prints JSON counts; the report file exists.
  - `test_report_shows_k_at_three_thresholds_with_and_without_winsorising`.
- [ ] **Step 2: FAIL. Step 3: Implement** the report (per split):
  - trials archived, without returns, flat, passive; K at 0.3/0.5/0.7, winsorised and raw;
  - cluster size distribution; the 15 largest clusters at 0.3 with family, size, members' strategy refs, best dev
    Sharpe;
  - where each seed instance falls (cluster at 0.3 and 0.7, cluster size, dev Sharpe, split DSR);
  - new-family proposals with their statistics, for the human to confirm or reject (R6 review: the trend
    concentration);
  - top 10 trials by dev Sharpe with split DSR and the legacy DSR side by side;
  - conclusions imported per topic and decision; citation failures.
  `zqr memory status` prints the latest clustering and K per split.
- [ ] **Step 4: PASS. Step 5: Commit.**

## Task 12: Daily report and library show

**Files:** Modify `desk/report.py`, `library/commands.py`; Test `tests/test_report.py`, `tests/test_library.py`.

- [ ] **Step 1: Failing tests**: the research section has a line per split
  `- <split>: <n> trials, effective K 34 / 113 / 255 (ρ 0.3 / 0.5 / 0.7), next split earliest 2027-10-12`;
  `zqr library show btc-tsmom-12m` shows the seed run's dev Sharpe and split DSR, or "not characterised".
- [ ] **Step 2: FAIL. Step 3: Implement** (read-only queries; no clustering → "K not computed").
- [ ] **Step 4: PASS. Step 5: Commit.**

## Task 13: Docs, full verification, release and production run

- [ ] `docs/development.md`: section 5.8 Research memory (A1-A7 in practice, commands, layout), layout table rows for
  `memory/` and `library/` under `ZQR_HOME`, test count.
- [ ] Full suite: `MPLCONFIGDIR=/tmp/mpl .venv/bin/pytest -q -p no:logging -p no:warnings` (outside the sandbox) and
  `.venv/bin/ruff check src tests`.
- [ ] Local rehearsal: `zqr release --target local`; on a copy of production `experiments.db` and `results/`
  (read-only source), run the six `zqr memory` steps and compare K with the 2026-10-13 feasibility run (expect the
  same order of magnitude: BTC about 250-270 at 0.7; differences come from the buy-and-hold regression replacing
  the principal-component stand-in and from the passive cluster).
- [ ] Ask the user before pushing and releasing (release carries the phase 0 debt fixes too). After release:
  `zqr backup`, then on WSL `zqr memory backfill`, `characterise`, `cluster`, `import-conclusions`, `archive`,
  `report`.
- [ ] **Checkpoint:** send the clustering report to the user: K per split, seed placement, family proposals,
  R6 (trend concentration) and the `short_term_reversal` family. Phase 3 starts after their review.

## Order and dependencies

Tasks 1-2 are independent; 3 needs 2; 4 needs 3; 5 needs 3; 6 needs 5; 7 needs 6; 8 needs 3 (and the seed catalog);
9 is independent; 10 needs 1, 6, 7, 9; 11 needs everything before it; 12 needs 6 and 8.

## Risks

- **Memory and time:** 900 trials × 1,267 days is about 9 MB of float64 per split; the correlation matrix
  900 × 900 is trivial; reading 1,788 equity files (30k rows each) takes about a minute and runs once (backfill).
- **Production disk:** archives and reports are small (under 50 MB in total).
- **The paper fleet is not touched;** research stays disabled. The release restarts the container (about 3 s,
  as on 2026-10-15).
- **K differs from the design's estimate:** the design used the first principal component as a stand-in for buy and
  hold. The report shows K with and without winsorising so the change can be judged.
