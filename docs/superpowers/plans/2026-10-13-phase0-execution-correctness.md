# Phase 0: Execution Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make paper trading reproduce the backtest's rules and survive outages, restarts and config edits, so paper results can be compared with research (design `docs/superpowers/specs/2026-10-13-research-library-portfolio-design.md`, section 7.2; review `docs/reviews/2026-10-13/zqr-project-review.md`, findings F1–F9).

**Architecture:** One rebalancing function shared by the backtest engine and the paper trader. A per-feed bar store (append-only parquet, anchored at the first bar of the frozen dataset) becomes the paper history: traders compute from the whole store, the poller backfills from the last stored bar, and the report recomputes decisions from it. Freshness uses bar close times. The fleet hot-updates risk limits, refuses identity changes, and retries failed attaches. Promotion carries the validated configuration and resumes from any intermediate state; every holdout path goes through one capped gate. Traders mark equity on every bar; the report's parity compares targets, orders and fills. The design asks each strategy to declare its history requirement; this plan instead gives every strategy the whole anchored store, which meets any requirement (fixed lookbacks and stateful rules alike) without per-strategy declarations, at a compute cost of a few milliseconds per bar for templates.

**Tech Stack:** Python 3.12, pandas, numpy, pydantic v2, SQLite, pytest, ruff, uv.

---

## Conventions

- Run tests with `uv run pytest <target> -q`; the full suite is `uv run pytest -q` (121 tests pass before this plan).
- Lint with `uv run ruff check .`.
- Commit after every task on `main` with the message given. Do not push until Task 13.
- Existing helpers used in tests: `tests/conftest.py` (`make_raw`, `HOUR`, `T0`, fixtures `paths`, `store`, `dataset`, `make_mandate`), `tests/test_paper.py` (`PRODUCT`, `_bars`, `_cfg`), `tests/test_fleet.py` (`FakeCoinbase`, `NOW0`, `_entry`, `_setup`, `_bump`), `tests/test_desk.py` (`EASY_HOLDOUT`, `_validated`, `_authorize`).

## File map

| File | Change |
|---|---|
| `src/quant_research/backtest/rebalance.py` | New: `needs_trade`, `EPS` |
| `src/quant_research/backtest/engine.py` | Use `needs_trade` |
| `src/quant_research/paper/trader.py` | Band via `needs_trade`; anchored history; bulk warm-up; `ingest`; freshness by bar close; `update_risk`; config identity; marks |
| `src/quant_research/paper/ledger.py` | `record_bars` (bulk), `marks` table |
| `src/quant_research/paper/feeds.py` | `range`, backfilling `poll`, empty responses, `SEED_BARS` |
| `src/quant_research/paper/barstore.py` | New: `BarStore`, `snapshot_seed` |
| `src/quant_research/paper/fleet.py` | Feeds on the bar store; catch-up; risk hot reload; identity refusal; attach retry; entry cost overrides; `entry_costs` |
| `src/quant_research/paper/runner.py` | Warm-up size when history is unbounded |
| `src/quant_research/paper/parity.py` | New: `paper_parity` |
| `src/quant_research/desk/promotion.py` | Carry configuration; refuse unsupported settings; resumable |
| `src/quant_research/validation/holdout.py` | Single capped gate, repeat reuse, override; `split_viewing_cap` |
| `src/quant_research/experiments/store.py` | `holdout_overrides` table |
| `src/quant_research/cli.py` | `holdout-test --override-reason`; split cap |
| `src/quant_research/desk/report.py` | Returns from marks; B&H with costs; new parity; override flag |
| `docs/development.md` | Paper section, holdout gate |

---

### Task 1: One rebalancing rule for backtest and paper (F1)

**Files:**
- Create: `src/quant_research/backtest/rebalance.py`
- Modify: `src/quant_research/backtest/engine.py:68-69,113-119`
- Modify: `src/quant_research/paper/trader.py` (`PaperConfig`, `_size`)
- Test: `tests/test_backtest.py`, `tests/test_paper.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backtest.py`:

```python
def test_needs_trade_rule():
    from quant_research.backtest.rebalance import needs_trade

    assert not needs_trade(0.5, 5_400.0, 10_000.0, tolerance=0.1, min_notional=1.0)
    assert needs_trade(0.5, 3_900.0, 10_000.0, tolerance=0.1, min_notional=1.0)
    assert needs_trade(0.0, 0.5, 10_000.0, tolerance=0.1, min_notional=1.0)  # a full exit ignores the band
    assert not needs_trade(0.0, 0.0, 10_000.0, tolerance=0.0, min_notional=1.0)
```

Append to `tests/test_paper.py` (add `from quant_research.portfolio.policy import PortfolioPolicy` to the imports):

```python
def test_paper_honours_the_band_like_the_backtest(tmp_path):
    raw = make_raw(24)
    px = np.linspace(100, 110, len(raw))
    for col in ("open", "high", "low", "close"):
        raw[col] = px
    bars, _ = validate_bars(raw, "coinbase", "BTC-USD", "ONE_HOUR", T0, T0 + 24 * HOUR)
    policy = PortfolioPolicy(asset_cap=0.5, band=0.1)
    cfg = _cfg(strategy_ref="template:buy_and_hold", parameters={}, policy=policy, fee_rate=Decimal(0),
               slippage_bps=Decimal(0), buy_cash_buffer=0)
    trader = run_replay(cfg, bars, tmp_path / "band", PRODUCT)
    bt = run_backtest(bars, np.full(len(bars), 0.5),
                      policy.engine_config(EngineConfig(costs=CostConfig(fee_rate=0, slippage_bps=0))))
    assert len(trader.ledger.fills()) == len(bt.fills) == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_backtest.py::test_needs_trade_rule tests/test_paper.py::test_paper_honours_the_band_like_the_backtest -q`
Expected: FAIL (`ModuleNotFoundError: quant_research.backtest.rebalance`; paper has 23 fills).

- [ ] **Step 3: Create the shared rule**

`src/quant_research/backtest/rebalance.py`:

```python
"""When a target becomes an order: one rule for the backtest engine and the paper trader."""

from __future__ import annotations

EPS = 1e-9  # relative to equity: below this a rebalance is floating-point noise


def needs_trade(target: float, position_value: float, equity: float, *, tolerance: float,
                min_notional: float) -> bool:
    """Trade when the target value differs from the position value by at least
    max(min_notional, tolerance * equity, EPS * equity), or when the target is 0 and a position is open."""
    if target == 0.0 and position_value > 0:
        return True
    threshold = max(min_notional, tolerance * equity, EPS * equity)
    return abs(target * equity - position_value) >= threshold
```

- [ ] **Step 4: Use it in the engine**

In `src/quant_research/backtest/engine.py`, add the import after `from quant_research.hashing import hash_obj`:

```python
from quant_research.backtest.rebalance import EPS as _EPS
from quant_research.backtest.rebalance import needs_trade
```

Delete the line `_EPS = 1e-9  # relative to equity: below this a rebalance is floating-point noise`.

Replace the block

```python
            equity_open = cash + qty * o[i]
            desired_value = target * equity_open
            delta = desired_value - qty * o[i]
            threshold = max(config.min_trade_notional, config.rebalance_tolerance * equity_open,
                            _EPS * equity_open)
            full_exit = target == 0.0 and qty > 0
            if full_exit or abs(delta) >= threshold:
```

with

```python
            equity_open = cash + qty * o[i]
            delta = target * equity_open - qty * o[i]
            full_exit = target == 0.0 and qty > 0
            if needs_trade(target, qty * o[i], equity_open, tolerance=config.rebalance_tolerance,
                           min_notional=config.min_trade_notional):
```

- [ ] **Step 5: Use it in the paper trader**

In `src/quant_research/paper/trader.py` add `from quant_research.backtest.rebalance import needs_trade` to the imports. In `PaperConfig`, after `buy_cash_buffer`, add:

```python
    min_trade_notional: float = 1.0  # the engine's default; skip smaller rebalances
```

At the top of `_size`, replace

```python
        p = self.product
        current_value = bal[p.base_currency] * price
        delta = D(decision.target_fraction) * equity - current_value
```

with

```python
        p = self.product
        current_value = bal[p.base_currency] * price
        if not needs_trade(decision.target_fraction, float(current_value), float(equity),
                           tolerance=self.cfg.policy.band, min_notional=self.cfg.min_trade_notional):
            return None
        delta = D(decision.target_fraction) * equity - current_value
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_backtest.py tests/test_paper.py -q`
Expected: all pass, including `test_paper_replay_matches_backtest`.

- [ ] **Step 7: Commit**

```bash
git add src/quant_research/backtest/rebalance.py src/quant_research/backtest/engine.py src/quant_research/paper/trader.py tests/test_backtest.py tests/test_paper.py
git commit -m "paper: apply the backtest's rebalancing rule, including the policy band (review F1)"
```

---

### Task 2: Anchored paper history with bulk warm-up (F2, trader side)

**Files:**
- Modify: `src/quant_research/paper/trader.py` (`PaperConfig.history_bars`, `_append_bar`, `warm_up`)
- Modify: `src/quant_research/paper/ledger.py` (add `record_bars`)
- Modify: `src/quant_research/paper/feeds.py` (add `SEED_BARS`)
- Modify: `src/quant_research/paper/runner.py:63`
- Test: `tests/test_paper.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_paper.py` (add `from quant_research.paper.trader import PaperConfig, target_fraction` in place of the existing `PaperConfig` import):

```python
def test_breakout_position_survives_long_history_and_restart(tmp_path):
    raw = make_raw(4000)
    for col in ("open", "high", "low", "close"):
        raw[col] = 100.0
        raw.loc[100:, col] = 110.0
    bars, _ = validate_bars(raw, "coinbase", "BTC-USD", "ONE_HOUR", T0, T0 + 4000 * HOUR)
    cfg = _cfg(strategy_ref="template:breakout", parameters={"entry": 48, "exit": 24})
    t1 = make_trader(cfg, tmp_path / "p", PRODUCT, clock=lambda: 0)
    t1.warm_up(bars.iloc[:3990])
    assert len(t1.history) == 3990
    assert target_fraction(t1.spec, cfg.policy, t1.history, cfg.granularity) == 1.0
    t1.ledger.close()
    t2 = make_trader(cfg, tmp_path / "p", PRODUCT, clock=lambda: 0)  # restart
    t2.warm_up(bars)
    assert len(t2.history) == 4000
    assert target_fraction(t2.spec, cfg.policy, t2.history, cfg.granularity) == 1.0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_paper.py::test_breakout_position_survives_long_history_and_restart -q`
Expected: FAIL (`assert 3000 == 3990`).

- [ ] **Step 3: Bulk bar recording in the ledger**

In `src/quant_research/paper/ledger.py`, after `record_bar`, add:

```python
    def record_bars(self, bars: pd.DataFrame) -> None:
        """Bulk `record_bar` in one transaction (warm-up loads years of bars)."""
        rows = [(str(b["bar_open_time"]), json.dumps(b, default=str)) for b in bars.to_dict("records")]
        self._conn.execute("BEGIN")
        self._conn.executemany("INSERT OR IGNORE INTO bars VALUES (?, ?)", rows)
        self._conn.execute("COMMIT")
```

- [ ] **Step 4: Unbounded history and bulk warm-up in the trader**

In `PaperConfig`, replace `history_bars: int = 3000` with:

```python
    history_bars: int | None = None  # None keeps every bar since the feed's anchor (stateful strategies need it)
```

Replace `_append_bar` and `warm_up` with:

```python
    def _append_bar(self, bar: dict[str, Any]) -> bool:
        row = pd.DataFrame([bar])
        if not self.history.empty:
            last = to_epoch(self.history["bar_open_time"])[-1]
            if to_epoch(row["bar_open_time"])[0] <= last:
                return False
        self.history = row if self.history.empty else pd.concat([self.history, row], ignore_index=True)
        if self.cfg.history_bars:
            self.history = self.history.tail(self.cfg.history_bars).reset_index(drop=True)
        return True

    def warm_up(self, bars: pd.DataFrame) -> None:
        """Load history without deciding; bars at or before the last known bar are ignored."""
        if not len(bars):
            return
        new = bars
        if not self.history.empty:
            new = bars[to_epoch(bars["bar_open_time"]) > to_epoch(self.history["bar_open_time"])[-1]]
        if len(new):
            self.history = (new.reset_index(drop=True) if self.history.empty
                            else pd.concat([self.history, new], ignore_index=True))
            if self.cfg.history_bars:
                self.history = self.history.tail(self.cfg.history_bars).reset_index(drop=True)
            self.ledger.record_bars(new)
        avail = float(to_epoch(bars["available_at"])[-1])
        if avail > (self.ledger.get("last_data_time") or 0):
            self.ledger.put("last_data_time", avail)
```

- [ ] **Step 5: Warm-up size for the single-trader live loop**

In `src/quant_research/paper/feeds.py`, after the imports, add:

```python
SEED_BARS = 17_520  # two years of hourly bars: history to fetch when no frozen dataset can seed it
```

In `src/quant_research/paper/runner.py`, change the import to `from quant_research.paper.feeds import SEED_BARS, CoinbaseCandlePoller, ReplayFeed, coinbase_mid_price` and replace

```python
    trader.warm_up(poller.history(min(cfg.history_bars, 1000)))
```

with

```python
    trader.warm_up(poller.history(cfg.history_bars or SEED_BARS))
```

- [ ] **Step 6: Run the paper, fleet and report tests**

Run: `uv run pytest tests/test_paper.py tests/test_fleet.py tests/test_report.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/quant_research/paper/trader.py src/quant_research/paper/ledger.py src/quant_research/paper/feeds.py src/quant_research/paper/runner.py tests/test_paper.py
git commit -m "paper: keep the whole history so stateful strategies survive window slides and restarts (review F2)"
```

---

### Task 3: Bar store and a poller that backfills (F2 store, F4)

**Files:**
- Create: `src/quant_research/paper/barstore.py`
- Modify: `src/quant_research/paper/feeds.py` (`CoinbaseCandlePoller`)
- Test: `tests/test_paper_data.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_paper_data.py`:

```python
from quant_research.data.validate import validate_bars
from quant_research.paper.barstore import BarStore
from quant_research.paper.feeds import CoinbaseCandlePoller
from tests.conftest import HOUR, T0, make_raw
from tests.test_fleet import NOW0, FakeCoinbase


def _hourly(n):
    bars, _ = validate_bars(make_raw(n), "coinbase", "BTC-USD", "ONE_HOUR", T0, T0 + n * HOUR)
    return bars


def test_bar_store_appends_only_newer_bars_and_reports_holes(tmp_path):
    bars = _hourly(10)
    store = BarStore(tmp_path / "b.parquet", "ONE_HOUR")
    assert len(store.append(bars.iloc[:6])) == 6
    assert len(store.append(bars.iloc[3:8])) == 2
    assert len(store.append(bars.iloc[9:])) == 1
    reopened = BarStore(tmp_path / "b.parquet", "ONE_HOUR")
    assert len(reopened.bars) == 9
    assert reopened.last_open() == T0 + 9 * HOUR
    assert reopened.missing_opens() == [T0 + 8 * HOUR]


def test_poller_backfills_after_an_outage():
    clock = {"now": float(NOW0)}
    poller = CoinbaseCandlePoller(FakeCoinbase(lambda: clock["now"]), "BTC-USD", "ONE_HOUR",
                                  clock=lambda: clock["now"])
    poller.history(20)
    last = poller.last_open
    clock["now"] += 10 * HOUR
    opens = [int(b["bar_open_time"].timestamp()) for b in poller.poll()]
    assert opens == [last + HOUR * k for k in range(1, 11)]
    assert poller.poll() == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_paper_data.py -q`
Expected: FAIL (`ModuleNotFoundError: quant_research.paper.barstore`).

- [ ] **Step 3: Create the bar store**

`src/quant_research/paper/barstore.py`:

```python
"""Completed bars for one product and granularity, anchored at the first stored bar and only ever appended.

Paper strategies compute from the whole store, so stateful rules (a breakout's open position) see the same history
on every restart, and the report can recompute any decision from it.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant_research.data.bars import to_epoch
from quant_research.data.snapshot import list_datasets, load_dataset
from quant_research.paths import Paths
from quant_research.timeutil import granularity_seconds


class BarStore:
    def __init__(self, path: Path, granularity: str):
        self.path = Path(path)
        self.step = granularity_seconds(granularity)
        self.bars = pd.read_parquet(self.path) if self.path.exists() else pd.DataFrame()

    @classmethod
    def for_feed(cls, paths: Paths, product: str, granularity: str) -> BarStore:
        return cls(paths.paper / "bars" / f"{product}-{granularity}.parquet", granularity)

    def last_open(self) -> int | None:
        return int(to_epoch(self.bars["bar_open_time"])[-1]) if len(self.bars) else None

    def append(self, bars: pd.DataFrame | None) -> pd.DataFrame:
        """Add bars newer than the last stored one; returns the rows added, in time order."""
        if bars is None or not len(bars):
            return pd.DataFrame()
        new = bars.drop_duplicates("bar_open_time").sort_values("bar_open_time")
        last = self.last_open()
        if last is not None:
            new = new[to_epoch(new["bar_open_time"]) > last]
        new = new.reset_index(drop=True)
        if len(new):
            self.bars = new if not len(self.bars) else pd.concat([self.bars, new], ignore_index=True)
            self._save()
        return new

    def missing_opens(self) -> list[int]:
        """Open times absent between the first and last stored bar (the venue returned nothing for them)."""
        if len(self.bars) < 2:
            return []
        opens = to_epoch(self.bars["bar_open_time"])
        expected = range(int(opens[0]), int(opens[-1]) + self.step, self.step)
        return sorted(set(expected) - set(opens.tolist()))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        self.bars.to_parquet(tmp, index=False)
        tmp.replace(self.path)


def snapshot_seed(paths: Paths, product: str, granularity: str) -> pd.DataFrame | None:
    """Bars of the most recent frozen dataset for this product and granularity, if any."""
    found = [m for m in list_datasets(paths)
             if m.get("instrument") == product and m.get("granularity") == granularity]
    if not found:
        return None
    latest = max(found, key=lambda m: m.get("requested_end") or 0)
    bars, _ = load_dataset(paths, latest["dataset_id"])
    return bars
```

- [ ] **Step 4: Make the poller backfill from the last emitted bar**

In `src/quant_research/paper/feeds.py`, replace the methods `history`, `_fetch` and `poll` of `CoinbaseCandlePoller` with:

```python
    def _end(self) -> int:
        now = int(self.clock())
        return now - now % self.step

    def range(self, start: int, end: int) -> pd.DataFrame:
        """Completed bars with open time in [start, end), paged in 350-bar requests."""
        frames = []
        while start < end:
            we = min(start + 350 * self.step, end)
            frames.append(self._fetch(start, we))
            start = we
        frames = [f for f in frames if len(f)]
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        return df.drop_duplicates("bar_open_time").sort_values("bar_open_time").reset_index(drop=True)

    def history(self, bars: int) -> pd.DataFrame:
        """The most recent `bars` completed bars."""
        end = self._end()
        df = self.range(end - bars * self.step, end)
        if len(df):
            self.last_open = int(to_epoch(df["bar_open_time"])[-1])
        return df

    def _fetch(self, start: int, end: int) -> pd.DataFrame:
        rec = self.client.candles(self.product_id, start, end - self.step, self.granularity)
        fetched_at = self.clock()
        candles = rec.body.get("candles", [])
        if not candles:
            return pd.DataFrame()
        raw = pd.DataFrame([{"start": int(c["start"]), "open": float(c["open"]), "high": float(c["high"]),
                             "low": float(c["low"]), "close": float(c["close"]), "volume": float(c["volume"]),
                             "source_version": SOURCE_VERSION, "fetched_at": fetched_at}
                            for c in candles])
        bars, _ = validate_bars(raw, VENUE, self.product_id, self.granularity, start, end)
        # Live bars become available when we observe them, not at their nominal close.
        bars["available_at"] = pd.to_datetime(fetched_at, unit="s", utc=True)
        return bars

    def poll(self) -> list[dict[str, Any]]:
        """Every completed bar after the last one emitted, in order; after an outage this backfills the gap."""
        end = self._end()
        start = end - self.lookback * self.step if self.last_open is None else self.last_open + self.step
        bars = self.range(start, end) if start < end else pd.DataFrame()
        if len(bars) and self.last_open is not None:
            bars = bars[to_epoch(bars["bar_open_time"]) > self.last_open]
        if not len(bars):
            return []
        self.last_open = int(to_epoch(bars["bar_open_time"])[-1])
        return bars.to_dict("records")
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_paper_data.py tests/test_paper.py tests/test_fleet.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/quant_research/paper/barstore.py src/quant_research/paper/feeds.py tests/test_paper_data.py
git commit -m "paper: append-only bar store; the poller backfills from the last bar after an outage (review F4)"
```

---

### Task 4: Fleet feeds on the bar store; catch-up bars only rebuild state (F2, F4)

**Files:**
- Modify: `src/quant_research/paper/fleet.py` (`_Feed`, `PaperFleet.__init__`, `sync_roster`, `step`)
- Modify: `src/quant_research/paper/trader.py` (add `ingest`)
- Test: `tests/test_fleet.py`, `tests/test_report.py`, `tests/test_paper_data.py`

- [ ] **Step 1: Pass a small seed in existing tests**

In `tests/test_fleet.py` and `tests/test_report.py`, add `seed_bars=1000` to every `PaperFleet(` call, e.g.

```python
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_fleet.py` (add `from quant_research.data.bars import to_epoch` and `from quant_research.paper.barstore import BarStore` to the imports):

```python
def test_fleet_catches_up_after_an_outage_and_trades_only_the_latest_bar(tmp_path):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    t["now"] += 10 * HOUR
    fleet.step()
    trader = fleet.traders["btc-sma"]
    opens = to_epoch(trader.history["bar_open_time"])
    assert (np.diff(opens) == HOUR).all()
    assert len(trader.ledger.decisions()) == 1
    store = BarStore.for_feed(paths, "BTC-USD", "ONE_HOUR")
    assert store.missing_opens() == [] and store.last_open() == opens[-1]
```

Append to `tests/test_paper_data.py` (add `from quant_research.data.bars import to_epoch` and `from quant_research.paper.fleet import _Feed`):

```python
def test_feed_is_anchored_at_the_frozen_dataset(paths, dataset):
    clock = {"now": float(NOW0)}
    feed = _Feed(paths, FakeCoinbase(lambda: clock["now"]), "BTC-USD", "ONE_HOUR", lambda: clock["now"],
                 seed_bars=10)
    opens = to_epoch(feed.history["bar_open_time"])
    assert opens[0] == T0
    assert opens[-1] == NOW0 - NOW0 % HOUR - HOUR
    assert (feed.store.missing_opens() == [])
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_fleet.py tests/test_paper_data.py -q`
Expected: FAIL (`seed_bars` unexpected keyword; `_Feed` signature).

- [ ] **Step 4: Add `ingest` to the trader**

In `src/quant_research/paper/trader.py`, after `warm_up`, add:

```python
    def ingest(self, bar: dict[str, Any]) -> bool:
        """Add a bar without deciding: catch-up bars after an outage rebuild state but never trade."""
        if not self._append_bar(bar):
            return False
        self.ledger.record_bar(bar)
        self.ledger.put("last_bar_open", str(bar["bar_open_time"]))
        return True
```

- [ ] **Step 5: Rebuild the fleet feed on the bar store**

In `src/quant_research/paper/fleet.py`:

Replace the import `from quant_research.paper.feeds import CoinbaseCandlePoller, coinbase_mid_price` with

```python
from quant_research.paper.barstore import BarStore, snapshot_seed
from quant_research.paper.feeds import SEED_BARS, CoinbaseCandlePoller, coinbase_mid_price
```

Delete `WARMUP_BARS = 1000`. Replace the `_Feed` class with:

```python
class _Feed:
    """One market feed: the bar store (seeded from the frozen dataset, else from the venue) plus a poller."""

    def __init__(self, paths, client, product: str, granularity: str, clock: Callable[[], float], seed_bars: int):
        self.product, self.granularity = product, granularity
        self.store = BarStore.for_feed(paths, product, granularity)
        self.poller = CoinbaseCandlePoller(client, product, granularity, clock=clock)
        if self.store.last_open() is None:
            seed = snapshot_seed(paths, product, granularity)
            self.store.append(seed if seed is not None and len(seed) else self.poller.history(seed_bars))
        self.poller.last_open = self.store.last_open()
        self.store.append(pd.DataFrame(self.poller.poll()))
        self.missing = len(self.store.missing_opens())

    @property
    def history(self) -> pd.DataFrame:
        return self.store.bars

    def poll(self) -> list[dict[str, Any]]:
        added = self.store.append(pd.DataFrame(self.poller.poll()))
        return added.to_dict("records") if len(added) else []
```

In `PaperFleet.__init__`, add a keyword parameter `seed_bars: int = SEED_BARS` after `on_activate` and store it: `self.seed_bars = seed_bars`.

In `sync_roster`, replace

```python
                    feed = self.feeds[(e.product, e.granularity)] = _Feed(self.client, e.product, e.granularity,
                                                                         self.clock)
```

with

```python
                    feed = self.feeds[(e.product, e.granularity)] = _Feed(self.paths, self.client, e.product,
                                                                         e.granularity, self.clock, self.seed_bars)
```

In `step`, replace

```python
            for name in members:
                trader = self.traders[name]
                for bar in bars:
                    try:
                        trader.on_bar(bar)
                    except Exception as exc:  # noqa: BLE001
                        self.errors[name] = f"{type(exc).__name__}: {exc}"
                        trader._log("ALERT", "bar_error", error=self.errors[name])
```

with

```python
            if len(bars) > 1:
                self._log("WARN", "caught_up", product=product, bars=len(bars))
            missing = len(feed.store.missing_opens()) if bars else feed.missing
            if missing > feed.missing:
                self._log("ALERT", "bar_gap", product=product, missing=missing - feed.missing)
                feed.missing = missing
            for name in members:
                trader = self.traders[name]
                try:
                    for bar in bars[:-1]:
                        trader.ingest(bar)
                    if bars:
                        trader.on_bar(bars[-1])
                except Exception as exc:  # noqa: BLE001
                    self.errors[name] = f"{type(exc).__name__}: {exc}"
                    trader._log("ALERT", "bar_error", error=self.errors[name])
```

- [ ] **Step 6: Adjust the two fleet tests that read warm-up bars from the ledger**

In `test_fleet_runs_many_strategies_on_shared_feeds`, replace `assert len(ledger.bars()) >= 1000` with `assert len(fleet.traders[name].history) >= 1000`.

In `test_recorded_window_reproduces_each_decision`, replace `bars = ledger.bars()` with `bars = fleet.feeds[("BTC-USD", "ONE_HOUR")].history`.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_fleet.py tests/test_paper_data.py tests/test_report.py tests/test_paper.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/quant_research/paper/fleet.py src/quant_research/paper/trader.py tests/test_fleet.py tests/test_report.py tests/test_paper_data.py
git commit -m "paper fleet: feeds on the anchored bar store; catch-up bars rebuild state, only the latest trades"
```

---

### Task 5: Freshness by bar close time (F3)

**Files:**
- Modify: `src/quant_research/paper/trader.py` (`warm_up`, `ingest`, `on_bar`, `write_health`)
- Test: `tests/test_paper.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_paper.py` (add `from types import SimpleNamespace` and `from quant_research.paper.feeds import CoinbaseCandlePoller`):

```python
def test_an_old_bar_fetched_now_is_stale(tmp_path):
    now = T0 + 10 * HOUR

    class OldCandles:
        def candles(self, *_):
            return SimpleNamespace(body={"candles": [{"start": now - 5 * HOUR, "open": 100, "high": 100,
                                                      "low": 100, "close": 100, "volume": 10}]})

    bar = CoinbaseCandlePoller(OldCandles(), "BTC-USD", "ONE_HOUR", clock=lambda: now).poll()[0]
    trader = make_trader(_cfg(strategy_ref="template:buy_and_hold", parameters={}), tmp_path / "stale", PRODUCT,
                         clock=lambda: now)
    record = trader.on_bar(bar)
    assert not record["decision"]["approved"] and record["intent"] is None
    health = trader.write_health()
    assert health["data_age_seconds"] == 4 * HOUR and health["stale"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_paper.py::test_an_old_bar_fetched_now_is_stale -q`
Expected: FAIL (`approved` is true).

- [ ] **Step 3: Track the latest bar close as data time**

In `src/quant_research/paper/trader.py`, add a helper after `target_fraction`:

```python
def _close_epoch(bar: dict[str, Any]) -> float:
    return float(to_epoch(pd.Series([bar["bar_close_time"]]))[0])
```

In `warm_up`, replace

```python
        avail = float(to_epoch(bars["available_at"])[-1])
        if avail > (self.ledger.get("last_data_time") or 0):
            self.ledger.put("last_data_time", avail)
```

with

```python
        close = float(to_epoch(bars["bar_close_time"])[-1])
        if close > (self.ledger.get("last_data_time") or 0):
            self.ledger.put("last_data_time", close)
```

In `ingest`, before `return True`, add `self.ledger.put("last_data_time", _close_epoch(bar))`.

In `on_bar`, replace

```python
        self.ledger.put("last_data_time", float(avail))
```

with

```python
        self.ledger.put("last_data_time", _close_epoch(bar))
        self.ledger.put("last_seen_time", float(avail))
```

and in the `RiskContext(...)` call replace `last_data_time=float(avail)` with `last_data_time=_close_epoch(bar)`.

In `write_health`, add `"last_seen": self.ledger.get("last_seen_time"),` to the `health` dict after `"data_age_seconds"`. From now on the ledger key `last_data_time` holds the close time of the latest completed bar, and `last_seen_time` the time it was fetched.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_paper.py tests/test_fleet.py tests/test_report.py -q`
Expected: all pass (`test_stale_data_and_kill_switch_block_new_exposure` still blocks: replay time is one hour after each bar's close).

- [ ] **Step 5: Commit**

```bash
git add src/quant_research/paper/trader.py tests/test_paper.py
git commit -m "paper: staleness is the age of the latest bar close, not of the last fetch (review F3)"
```

---

### Task 6: Risk edits reach running traders; identity edits are refused (F5)

**Files:**
- Modify: `src/quant_research/paper/trader.py` (`PaperConfig.identity_hash`, `PaperTrader.update_risk`, `write_health`)
- Modify: `src/quant_research/paper/fleet.py` (`sync_roster`, new `_refresh`)
- Test: `tests/test_fleet.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_fleet.py`:

```python
def test_risk_edits_reach_running_traders_and_identity_edits_are_refused(tmp_path):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    r = load_roster(roster)
    r.risk.max_order_notional = 100
    save_roster(roster, r)
    _bump(roster, NOW0 + 1)
    fleet.step()
    trader = fleet.traders["btc-sma"]
    assert trader.cfg.risk.max_order_notional == 100 and trader.risk.limits.max_order_notional == 100
    r.entries[0].parameters = {"fast": 6, "slow": 20}
    save_roster(roster, r)
    _bump(roster, NOW0 + 2)
    health = fleet.step()
    assert fleet.traders["btc-sma"].cfg.parameters == {"fast": 5, "slow": 20}
    assert health["errors"]["btc-sma"].startswith("config change refused")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_fleet.py::test_risk_edits_reach_running_traders_and_identity_edits_are_refused -q`
Expected: FAIL (running limit stays 5000).

- [ ] **Step 3: Config identity and risk update in the trader**

In `src/quant_research/paper/trader.py` add `from quant_research.hashing import hash_obj` to the imports. In `PaperConfig`, add:

```python
    def identity_hash(self) -> str:
        """What is traded and how it is costed; a change needs a new strategy name. Risk limits are excluded."""
        return hash_obj(self.model_dump(mode="json", exclude={"risk", "sandbox"}))[:12]
```

In `PaperTrader`, after `recover`, add:

```python
    def update_risk(self, limits: RiskLimits) -> None:
        self.cfg = self.cfg.model_copy(update={"risk": limits})
        self.risk = RiskManager(limits)
        self._log("INFO", "risk_limits_updated", limits=limits.model_dump())
```

In `write_health`, add to the `health` dict: `"config_hash": self.cfg.identity_hash(), "risk": self.cfg.risk.model_dump(),`.

- [ ] **Step 4: Refresh existing traders in the fleet**

In `src/quant_research/paper/fleet.py`, in `sync_roster`, replace

```python
            if name in self.traders:
                continue
```

with

```python
            if name in self.traders:
                self._refresh(name, e)
                continue
```

and add the method after `sync_roster`:

```python
    def _refresh(self, name: str, e: RosterEntry) -> None:
        """Risk limits update in place; anything else that changes what is traded is refused."""
        trader, want = self.traders[name], self._config(e)
        running = trader.cfg.identity_hash()
        if want.identity_hash() != running:
            if not self.errors.get(name, "").startswith("config change refused"):
                self.errors[name] = (f"config change refused: running {running}, roster {want.identity_hash()}; "
                                     "add the changed strategy under a new name")
                self._log("ALERT", "config_change_refused", name=name, running=running,
                          wanted=want.identity_hash())
            return
        if self.errors.get(name, "").startswith("config change refused"):
            self.errors.pop(name)
        self.entries[name] = e
        if want.risk != trader.cfg.risk:
            trader.update_risk(want.risk)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_fleet.py tests/test_report.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/quant_research/paper/trader.py src/quant_research/paper/fleet.py tests/test_fleet.py
git commit -m "paper fleet: risk edits apply to running traders; strategy or cost edits are refused (review F5)"
```

---

### Task 7: Failed attaches retry with backoff (F7)

**Files:**
- Modify: `src/quant_research/paper/fleet.py` (`PaperFleet.__init__`, `sync_roster`)
- Test: `tests/test_fleet.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_fleet.py`:

```python
def test_a_failed_attach_is_retried_without_a_roster_edit(tmp_path):
    class Flaky(FakeCoinbase):
        attempts = 0

        def candles(self, *args):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("one-off outage")
            return super().candles(*args)

    t, paths, roster, _ = _setup(tmp_path, [_entry("btc-sma")])
    fleet = PaperFleet(paths, roster, Flaky(lambda: t["now"]), clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    assert "btc-sma" in fleet.errors and not fleet.traders
    t["now"] += 30
    fleet.step()
    assert not fleet.traders  # backing off
    t["now"] += 31
    fleet.step()
    assert "btc-sma" in fleet.traders and "btc-sma" not in fleet.errors
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_fleet.py::test_a_failed_attach_is_retried_without_a_roster_edit -q`
Expected: FAIL (never retried).

- [ ] **Step 3: Split roster reload from attaching**

In `PaperFleet.__init__`, add:

```python
        self._retry_at: dict[str, float] = {}
        self._failures: dict[str, int] = {}
```

Replace the whole `sync_roster` method with:

```python
    def sync_roster(self) -> None:
        self._reload_roster()
        self._attach_missing()

    def _reload_roster(self) -> None:
        try:
            mtime = self.roster_path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime == self._roster_mtime:
            return
        try:
            self.roster = load_roster(self.roster_path)
        except Exception as exc:  # noqa: BLE001 - a bad edit keeps the running set
            self._log("ALERT", "roster_invalid", error=f"{type(exc).__name__}: {exc}")
            return
        self._roster_mtime = mtime
        wanted = self.roster.enabled()
        for name in [n for n in self.traders if n not in wanted]:
            self.traders.pop(name).ledger.close()
            self.entries.pop(name, None)
            self._log("INFO", "strategy_detached", name=name)
        for name in [n for n in self._retry_at if n not in wanted]:
            self._retry_at.pop(name)
            self._failures.pop(name, None)
            self.errors.pop(name, None)
        for name, e in wanted.items():
            if name in self.traders:
                self._refresh(name, e)

    def _attach_missing(self) -> None:
        now = self.clock()
        for name, e in self.roster.enabled().items():
            if name in self.traders or now < self._retry_at.get(name, 0.0):
                continue
            try:
                feed = self.feeds.get((e.product, e.granularity))
                if feed is None:
                    feed = self.feeds[(e.product, e.granularity)] = _Feed(self.paths, self.client, e.product,
                                                                         e.granularity, self.clock, self.seed_bars)
                trader = make_trader(self._config(e), self.workdir / name, self._product(e.product),
                                     paths=self.paths, clock=self.clock)
                trader.recover()
                trader.warm_up(feed.history)
                trader.write_health()
            except Exception as exc:  # noqa: BLE001
                n = self._failures[name] = self._failures.get(name, 0) + 1
                self._retry_at[name] = now + min(60.0 * 2 ** (n - 1), 1800.0)
                self.errors[name] = f"{type(exc).__name__}: {exc}"
                self._log("ALERT", "strategy_attach_failed", name=name, error=self.errors[name], attempt=n)
                continue
            self.traders[name], self.entries[name] = trader, e
            self.errors.pop(name, None)
            self._retry_at.pop(name, None)
            self._failures.pop(name, None)
            self._log("INFO", "strategy_attached", name=name, strategy=e.strategy_ref, product=e.product)
            if self.on_activate is not None:
                try:
                    self.on_activate(e)
                except Exception as exc:  # noqa: BLE001
                    self._log("WARN", "activate_hook_failed", name=name, error=f"{type(exc).__name__}: {exc}")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_fleet.py tests/test_report.py -q`
Expected: all pass (`test_one_failing_strategy_does_not_stop_the_others` still sees `broken` in errors).

- [ ] **Step 5: Commit**

```bash
git add src/quant_research/paper/fleet.py tests/test_fleet.py
git commit -m "paper fleet: retry failed attaches with backoff instead of waiting for a roster edit (review F7)"
```

---

### Task 8: Promotion carries the validated configuration (F6)

**Files:**
- Modify: `src/quant_research/paper/fleet.py` (`RosterEntry`, `entry_costs`, `_config`)
- Modify: `src/quant_research/desk/promotion.py`
- Test: `tests/test_fleet.py`, `tests/test_desk.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fleet.py` (add `from decimal import Decimal`):

```python
def test_entry_costs_override_the_roster_defaults(tmp_path):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma", initial_cash=20_000.0, fee_rate=0.002,
                                                        slippage_bps=3.0, min_trade_notional=5.0)])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    cfg = fleet.traders["btc-sma"].cfg
    assert (cfg.initial_quote, cfg.fee_rate, cfg.slippage_bps, cfg.min_trade_notional) == (
        Decimal("20000"), Decimal("0.002"), Decimal("3.0"), 5.0)
```

Append to `tests/test_desk.py`:

```python
def test_promotion_carries_the_validated_capital_and_costs(paths, store, dataset):
    engine = {"initial_cash": 20_000.0, "costs": {"fee_rate": 0.001, "slippage_bps": 2.0}}
    cid = _validated(paths, store, dataset, holdout=EASY_HOLDOUT, engine_config=engine)
    _authorize(paths)
    assert promotion.promote(paths, store, cid)["stopped"] == "done"
    entry = load_roster(promotion.roster_path(paths)).entries[-1]
    assert (entry.initial_cash, entry.fee_rate, entry.slippage_bps) == (20_000.0, 0.001, 2.0)


def test_promotion_refuses_settings_paper_cannot_reproduce(paths, store, dataset):
    cid = _validated(paths, store, dataset, holdout=EASY_HOLDOUT, engine_config={"execution_delay_bars": 1})
    _authorize(paths)
    out = promotion.promote(paths, store, cid)
    assert "execution_delay_bars=1" in out["stopped"] and out["state"] == "validated"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_fleet.py::test_entry_costs_override_the_roster_defaults tests/test_desk.py -k "carries or cannot_reproduce" -q`
Expected: FAIL (`RosterEntry` has no `fee_rate`).

- [ ] **Step 3: Per-entry costs in the roster**

In `src/quant_research/paper/fleet.py`, in `RosterEntry`, after `initial_cash`, add:

```python
    fee_rate: float | None = None  # None = the roster's default
    slippage_bps: float | None = None
    min_trade_notional: float | None = None
```

After the `Roster` class, add:

```python
def entry_costs(e: RosterEntry, roster: Roster) -> tuple[float, float, float]:
    """(fee rate, slippage bps, minimum trade notional) for an entry: its own values, else the roster's."""
    return (roster.fee_rate if e.fee_rate is None else e.fee_rate,
            roster.slippage_bps if e.slippage_bps is None else e.slippage_bps,
            1.0 if e.min_trade_notional is None else e.min_trade_notional)
```

Replace `_config` with:

```python
    def _config(self, e: RosterEntry) -> PaperConfig:
        r = self.roster
        fee, slip, min_notional = entry_costs(e, r)
        sandbox = SandboxConfig(isolation="docker" if e.strategy_ref.startswith("plugin:") else "subprocess")
        return PaperConfig(name=e.name, instrument=e.product, granularity=e.granularity, strategy_ref=e.strategy_ref,
                           parameters=e.parameters, policy=e.policy, initial_quote=Decimal(str(e.initial_cash)),
                           fee_rate=Decimal(str(fee)), slippage_bps=Decimal(str(slip)),
                           min_trade_notional=min_notional,
                           risk=r.risk.model_copy(update={"kill_switch_file": str(self.workdir / e.name / "KILL")}),
                           sandbox=sandbox)
```

- [ ] **Step 4: Promotion copies the engine configuration and refuses what paper cannot run**

In `src/quant_research/desk/promotion.py`, add `from quant_research.backtest.engine import EngineConfig` to the imports and, after `PAPER_STATES`, add:

```python
# Engine settings the paper trader cannot reproduce; a candidate validated with them is not promoted.
PAPER_UNSUPPORTED = ("execution_delay_bars", "adverse_fill", "rebalance_tolerance")


def paper_gaps(engine: EngineConfig) -> list[str]:
    default = EngineConfig()
    return [f"{k}={getattr(engine, k)!r}" for k in PAPER_UNSUPPORTED if getattr(engine, k) != getattr(default, k)]
```

In `promote`, right after the `auth.check(...)` try/except block, add:

```python
    gaps = paper_gaps(mandate.engine_config)
    if gaps:
        return stop("paper cannot run the validated configuration (" + ", ".join(gaps)
                    + "); revalidate without these settings")
```

Replace the `RosterEntry(...)` construction with:

```python
    engine = mandate.engine_config
    entry = RosterEntry(name=f"{manifest['instrument'].split('-')[0].lower()}-{candidate_id}",
                        product=manifest["instrument"], granularity=manifest["granularity"],
                        strategy_ref=cand["strategy_ref"], parameters=cand["parameters"], policy=mandate.policy,
                        initial_cash=engine.initial_cash, fee_rate=engine.costs.fee_rate,
                        slippage_bps=engine.costs.slippage_bps, min_trade_notional=engine.min_trade_notional,
                        source="candidate", candidate_id=candidate_id)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_fleet.py tests/test_desk.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/quant_research/paper/fleet.py src/quant_research/desk/promotion.py tests/test_fleet.py tests/test_desk.py
git commit -m "promotion: paper runs the validated capital and costs; refuse settings paper cannot reproduce (review F6)"
```

---

### Task 9: One capped holdout gate (F9)

**Files:**
- Modify: `src/quant_research/experiments/store.py` (schema, override methods)
- Modify: `src/quant_research/validation/holdout.py`
- Modify: `src/quant_research/cli.py` (`cmd_holdout`, `holdout-test` parser)
- Modify: `src/quant_research/desk/report.py` (`research_section`)
- Test: `tests/test_experiments.py`

- [ ] **Step 1: Write the failing tests and update the existing ones**

In `tests/test_experiments.py`, change the import to `from quant_research.validation.holdout import HoldoutError, run_holdout, split_viewing_cap`. In `test_holdout_requires_freeze_and_tracks_viewings` and `test_holdout_dsr_counts_viewings_not_research_trials`, add `max_viewings=10` to every `run_holdout(...)` call. In `test_holdout_requires_freeze_and_tracks_viewings`, replace

```python
    _, prior2 = run_holdout(paths, store, req)
    assert prior2 == 1
```

with

```python
    again, prior2 = run_holdout(paths, store, req, max_viewings=10)
    assert again.status == "duplicate" and prior2 == 0  # the same candidate again is not a new viewing
    assert len(store.holdout_runs(split.split_id)) == 1
```

Append:

```python
def test_holdout_cap_applies_to_every_path_and_overrides_are_recorded(paths, store, dataset):
    manifest, split = dataset
    reqs = [_req(manifest, split, parameters={"fast": f, "slow": 50}) for f in (5, 8, 12)]
    for r in reqs:
        spec = r.spec.normalized()
        store.freeze_candidate(spec.candidate_hash(), spec.strategy_ref, spec.parameters)
    run_holdout(paths, store, reqs[0], max_viewings=2)
    run_holdout(paths, store, reqs[1], max_viewings=2)
    again, _ = run_holdout(paths, store, reqs[0], max_viewings=2)
    assert again.status == "duplicate" and len(store.holdout_runs(split.split_id)) == 2
    with pytest.raises(HoldoutError, match="already viewed 2"):
        run_holdout(paths, store, reqs[2], max_viewings=2)
    run_holdout(paths, store, reqs[2], max_viewings=2, override_reason="manual check")
    assert len(store.holdout_runs(split.split_id)) == 3
    assert store.holdout_overrides(split.split_id)[0]["reason"] == "manual check"


def test_split_viewing_cap_uses_the_tightest_mandate(store, dataset):
    from tests.conftest import make_mandate

    _, split = dataset
    assert split_viewing_cap(store, split.split_id) == 10
    for cap in (4, 2):
        m = make_mandate(dataset, holdout={"max_viewings": cap, "min_primary": 0.0, "min_total_return": 0.0},
                         question=f"cap {cap}")
        store.put_mandate(m.mandate_id(), m.model_dump(mode="json"))
    assert split_viewing_cap(store, split.split_id) == 2
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_experiments.py -q`
Expected: FAIL (`split_viewing_cap` not importable).

- [ ] **Step 3: Store overrides**

In `src/quant_research/experiments/store.py`, add to `SCHEMA` after the `holdout_runs` table:

```sql
CREATE TABLE IF NOT EXISTS holdout_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT, split_id TEXT, experiment_id TEXT, reason TEXT, at REAL
);
```

After `record_holdout_run`, add:

```python
    def record_holdout_override(self, split_id: str, experiment_id: str, reason: str) -> None:
        self._insert("holdout_overrides", {"split_id": split_id, "experiment_id": experiment_id, "reason": reason,
                                           "at": time.time()})

    def holdout_overrides(self, split_id: str) -> list[dict[str, Any]]:
        rows = self._exec("SELECT * FROM holdout_overrides WHERE split_id=? ORDER BY at", (split_id,)).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: The gate**

In `src/quant_research/validation/holdout.py`, replace `run_holdout` with the code below and add `DEFAULT_MAX_VIEWINGS` and `split_viewing_cap` above it:

```python
DEFAULT_MAX_VIEWINGS = 10  # per split, when no mandate on the split declares a limit


def split_viewing_cap(store: ExperimentStore, split_id: str) -> int:
    """The tightest viewing limit any mandate on this split declares; DEFAULT_MAX_VIEWINGS if none does."""
    from quant_research.research.mandate import Mandate

    caps = [Mandate.model_validate(m["body"]).holdout.max_viewings for m in store.list_mandates()
            if m["body"].get("split_id") == split_id]
    return min(caps) if caps else DEFAULT_MAX_VIEWINGS


def run_holdout(
    paths: Paths,
    store: ExperimentStore,
    request: ExperimentRequest,
    sandbox: SandboxConfig | None = None,
    *,
    max_viewings: int,
    override_reason: str | None = None,
) -> tuple[ExperimentResult, int]:
    """The single gate to holdout data; returns the result and the viewings recorded before this candidate's.

    A candidate already viewed on the split gets its recorded result back without a new viewing. Otherwise the
    split must have fewer than `max_viewings` viewings, unless `override_reason` is given; an override is recorded
    and marks the split's holdout as contaminated.
    """
    spec = request.spec.normalized()
    chash = spec.candidate_hash()
    if not store.is_frozen(chash):
        raise HoldoutError("candidate is not frozen; run `zqr freeze-strategy` before the holdout test")
    runs = store.holdout_runs(request.split_id)
    earlier = next((r for r in runs if r["candidate_hash"] == chash), None)
    over = len(runs) >= max_viewings
    if earlier is None and over and not override_reason:
        raise HoldoutError(f"holdout of {request.split_id} already viewed {len(runs)} time(s); the limit is "
                           f"{max_viewings}")
    data = load_holdout_data(paths, request.split_id)
    result = run_experiment(paths, store, request.model_copy(update={"parameters": spec.parameters}),
                            kind="holdout", data=data, sandbox=sandbox, gate=GateContext(caller="holdout"))
    if earlier is not None and result.status == "duplicate":
        store.set_notes(result.experiment_id, f"repeat of holdout run {earlier['experiment_id']}; not a new viewing")
        return result, earlier["prior_runs_on_split"]
    prior = store.record_holdout_run(request.split_id, chash, result.experiment_id)
    if over:
        reason = override_reason or "candidate re-run with a different request after the limit"
        store.record_holdout_override(request.split_id, result.experiment_id, reason)
        note = f"over the viewing limit ({reason}); this split's holdout is contaminated"
    elif prior == 0:
        note = "first holdout viewing on this split"
    else:
        note = f"holdout already viewed {prior} time(s) on this split; result is no longer an untouched test"
    store.set_notes(result.experiment_id, note)
    return result, prior
```

In `run_candidate_holdout`, delete the block

```python
    viewed = len(store.holdout_runs(mandate.split_id))
    if viewed >= mandate.holdout.max_viewings:
        raise HoldoutError(f"holdout of {mandate.split_id} already viewed {viewed} time(s); the mandate allows "
                           f"{mandate.holdout.max_viewings}")
```

and change the call to `result, prior = run_holdout(paths, store, request, sandbox, max_viewings=mandate.holdout.max_viewings)`.

- [ ] **Step 5: CLI**

In `src/quant_research/cli.py`, in `cmd_holdout`, change the import to `from quant_research.validation.holdout import run_holdout, split_viewing_cap` and replace

```python
    res, prior = run_holdout(paths, store, _request(args, paths), sandbox=_sandbox(args))
```

with

```python
    res, prior = run_holdout(paths, store, _request(args, paths), sandbox=_sandbox(args),
                             max_viewings=split_viewing_cap(store, args.split), override_reason=args.override_reason)
```

After `p.add_argument("--candidate", ...)` of `holdout-test`, add:

```python
    p.add_argument("--override-reason", help="exceed the split's viewing limit; recorded, and marks the split's "
                   "holdout as contaminated")
```

- [ ] **Step 6: Report**

In `src/quant_research/desk/report.py`, in `research_section`, replace

```python
                         f"{left}{' **(exhausted: no more automatic promotion)**' if left <= 0 else ''} |")
```

with

```python
                         f"{left}{' **(exhausted: no more automatic promotion)**' if left <= 0 else ''}"
                         f"{' **(contaminated by override)**' if store.holdout_overrides(m.split_id) else ''} |")
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_experiments.py tests/test_desk.py tests/test_report.py tests/test_m3.py tests/test_contracts_v2.py -q`
Expected: all pass (`test_promotion_respects_product_cap_and_holdout_limit` still sees "already viewed").

- [ ] **Step 8: Commit**

```bash
git add src/quant_research/experiments/store.py src/quant_research/validation/holdout.py src/quant_research/cli.py src/quant_research/desk/report.py tests/test_experiments.py
git commit -m "holdout: one capped gate for every path; repeats are not viewings; overrides are recorded (review F9)"
```

---

### Task 10: Resumable promotion that checks slots before the holdout (F8)

**Files:**
- Modify: `src/quant_research/desk/promotion.py` (`promote`)
- Test: `tests/test_desk.py`

- [ ] **Step 1: Write the failing tests and update the cap test**

In `tests/test_desk.py`, add `save_roster` to the import from `quant_research.paper.fleet`. In `test_promotion_respects_product_cap_and_holdout_limit`, replace

```python
    assert "authorization cap" in out["stopped"] and out["state"] == "final_evaluated"
```

with

```python
    assert "authorization cap" in out["stopped"] and out["state"] == "validated"  # no holdout viewing spent
```

and insert `_authorize(paths, max_paper_strategies=3)` on the line before `one_view = {...}`.

Append:

```python
def test_promotion_resumes_after_a_slot_frees(paths, store, dataset):
    _authorize(paths, max_paper_strategies=1)
    first = _validated(paths, store, dataset, holdout=EASY_HOLDOUT)
    assert promotion.promote(paths, store, first)["stopped"] == "done"
    second = _validated(paths, store, dataset, template="momentum", params={"lookback": 24}, holdout=EASY_HOLDOUT)
    out = promotion.promote(paths, store, second)
    assert "authorization cap" in out["stopped"] and out["state"] == "validated"
    viewings = len(store.holdout_runs(dataset[1].split_id))
    rp = promotion.roster_path(paths)
    roster = load_roster(rp)
    roster.entries[0].enabled = False
    save_roster(rp, roster)
    out = promotion.promote(paths, store, second)
    assert out["stopped"] == "done" and out["state"] == "paper_approved"
    assert len(store.holdout_runs(dataset[1].split_id)) == viewings + 1


def test_promotion_is_idempotent_from_paper_approved(paths, store, dataset):
    _authorize(paths)
    cid = _validated(paths, store, dataset, holdout=EASY_HOLDOUT)
    assert promotion.promote(paths, store, cid)["stopped"] == "done"
    rp = promotion.roster_path(paths)
    roster = load_roster(rp)
    roster.entries = [e for e in roster.entries if e.candidate_id != cid]  # crashed before the roster write
    save_roster(rp, roster)
    viewings = len(store.holdout_runs(dataset[1].split_id))
    out = promotion.promote(paths, store, cid)
    assert out["stopped"] == "done" and out["steps"] == ["roster"]
    assert load_roster(rp).entries[-1].candidate_id == cid
    assert len(store.holdout_runs(dataset[1].split_id)) == viewings
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_desk.py -k promotion -q`
Expected: FAIL (`state` is `final_evaluated`; `not validated (state paper_approved)`).

- [ ] **Step 3: Rewrite `promote` as a resumable sequence**

In `src/quant_research/desk/promotion.py`, after `PAPER_UNSUPPORTED`/`paper_gaps`, add `RESUMABLE = ("validated", "frozen", "final_evaluated", "paper_approved")` and replace `promote` with:

```python
def promote(paths: Paths, store: ExperimentStore, candidate_id: str, sandbox: SandboxConfig | None = None,
            now: float | None = None) -> dict[str, Any]:
    """Push a candidate as far as the authorization and the mandate allow, resuming from the state it reached.

    Paper slots are checked before the holdout so a full roster never spends a viewing; a frozen candidate whose
    holdout already ran gets that result back (see `run_holdout`); a paper_approved candidate only needs its
    roster entry.
    """
    out: dict[str, Any] = {"candidate_id": candidate_id, "steps": []}

    def stop(reason: str) -> dict[str, Any]:
        cand = store.get_candidate(candidate_id)
        return out | {"state": cand["state"] if cand else None, "stopped": reason}

    def state() -> str:
        return store.get_candidate(candidate_id)["state"]

    cand = store.get_candidate(candidate_id)
    if cand is None or cand["state"] not in RESUMABLE:
        return stop(f"nothing to promote (state {cand['state'] if cand else 'unknown'})")
    auth = auth_mod.load(authorization_path(paths))
    if auth is None:
        return stop("no standing authorization; waiting for a human (`zqr candidate freeze`)")
    mandate = Mandate.model_validate(store.get_mandate(cand["mandate_id"])["body"])
    manifest = load_manifest(paths, mandate.dataset_id)
    try:
        auth.check(now=now, product=manifest["instrument"])
    except AuthorizationError as exc:
        return stop(str(exc))
    gaps = paper_gaps(mandate.engine_config)
    if gaps:
        return stop("paper cannot run the validated configuration (" + ", ".join(gaps)
                    + "); revalidate without these settings")
    full = f"paper roster already has {auth.max_paper_strategies} candidate strategies (authorization cap)"

    try:
        if state() != "paper_approved" and _paper_candidates(paths) >= auth.max_paper_strategies:
            return stop(full)
        if state() == "validated":
            freeze(paths, store, candidate_id, "desk standing authorization", actor="desk", authorization=auth)
            out["steps"].append("frozen")
        if state() == "frozen":
            res = run_candidate_holdout(paths, store, candidate_id, sandbox)
            out["steps"].append("final_evaluated" if res["passed"] else "failed_final")
            out["holdout"] = {k: v for k, v in res["candidate"]["evidence"].items() if k != "rule"}
            if not res["passed"]:
                return stop("holdout pass rule not met")
        if state() == "final_evaluated":
            if _paper_candidates(paths) >= auth.max_paper_strategies:
                return stop(full)
            transition(store, candidate_id, "paper_approved", "desk", note="desk standing authorization",
                       authorization=auth)
            out["steps"].append("paper_approved")
    except (RegistryError, HoldoutError) as exc:
        return stop(str(exc))

    engine = mandate.engine_config
    entry = RosterEntry(name=f"{manifest['instrument'].split('-')[0].lower()}-{candidate_id}",
                        product=manifest["instrument"], granularity=manifest["granularity"],
                        strategy_ref=cand["strategy_ref"], parameters=cand["parameters"], policy=mandate.policy,
                        initial_cash=engine.initial_cash, fee_rate=engine.costs.fee_rate,
                        slippage_bps=engine.costs.slippage_bps, min_trade_notional=engine.min_trade_notional,
                        source="candidate", candidate_id=candidate_id)
    if add_entry(roster_path(paths), entry):
        out["steps"].append("roster")
    out["roster_name"] = entry.name
    return stop("done")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_desk.py -q`
Expected: all pass (`test_promotion_runs_to_the_roster_and_activates` still lists `["frozen", "final_evaluated", "paper_approved", "roster"]`).

- [ ] **Step 5: Commit**

```bash
git add src/quant_research/desk/promotion.py tests/test_desk.py
git commit -m "promotion: resumable from any intermediate state; slots checked before the holdout (review F8)"
```

---

### Task 11: Mark equity on every bar; report returns from marks

**Files:**
- Modify: `src/quant_research/paper/ledger.py` (`marks` table, `record_mark`, `marks`)
- Modify: `src/quant_research/paper/trader.py` (`_mark`, calls in `on_bar` and `ingest`)
- Modify: `src/quant_research/desk/report.py` (`paper_section`)
- Test: `tests/test_paper.py`, `tests/test_report.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_paper.py`:

```python
def test_every_bar_is_marked_even_without_a_rebalance(tmp_path):
    bars = _bars(60)
    trader = run_replay(_cfg(policy=PortfolioPolicy(rebalance_every=24)), bars, tmp_path / "m", PRODUCT)
    marks = trader.ledger.marks()
    assert len(marks) == len(bars) and len(trader.ledger.decisions()) < len(bars)
    final_close = Decimal(str(bars["close"].iloc[-1]))
    assert marks.iloc[-1]["equity"] == pytest.approx(float(trader.equity(final_close)))
```

Append to `tests/test_report.py`:

```python
def test_report_day_return_comes_from_marks(tmp_path):
    from quant_research.desk import promotion
    from quant_research.desk.report import paper_section
    from quant_research.paper.fleet import Roster, RosterEntry, save_roster
    from quant_research.paper.runner import run_replay
    from quant_research.paths import get_paths
    from quant_research.portfolio.policy import PortfolioPolicy
    from tests.conftest import T0
    from tests.test_paper import PRODUCT, _bars, _cfg

    paths = get_paths(tmp_path / "home")
    policy = PortfolioPolicy(rebalance_every=24)
    entry = RosterEntry(name="btc-sma", product="BTC-USD", strategy_ref="template:sma_cross",
                        parameters={"fast": 5, "slow": 20}, policy=policy)
    save_roster(promotion.roster_path(paths), Roster(fee_rate=0.001, slippage_bps=2, entries=[entry]))
    trader = run_replay(_cfg(name="btc-sma", policy=policy), _bars(72), paths.paper / "btc-sma", PRODUCT)
    marks = trader.ledger.marks()
    trader.ledger.close()
    lines, summary = paper_section(paths, "2024-01-02")
    start = marks[marks["at"] <= T0 + 24 * HOUR].iloc[-1]
    end = marks[marks["at"] <= T0 + 48 * HOUR].iloc[-1]
    assert summary["strategies"] == 1
    assert f"{end['equity'] / start['equity'] - 1:+.2%}" in "\n".join(lines)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_paper.py::test_every_bar_is_marked_even_without_a_rebalance tests/test_report.py::test_report_day_return_comes_from_marks -q`
Expected: FAIL (`Ledger` has no `marks`).

- [ ] **Step 3: Marks in the ledger**

In `src/quant_research/paper/ledger.py`, add to `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS marks (bar_open TEXT PRIMARY KEY, at REAL, price TEXT, base TEXT, quote TEXT,
                                  equity TEXT);
```

After `bars`, add:

```python
    def record_mark(self, bar_open: str, at: float, price: Decimal, base: Decimal, quote: Decimal) -> None:
        """Mark-to-market at a bar's close, whether or not the trader rebalanced."""
        self._conn.execute("INSERT OR REPLACE INTO marks VALUES (?,?,?,?,?,?)",
                           (bar_open, at, str(price), str(base), str(quote), str(quote + base * price)))

    def marks(self) -> pd.DataFrame:
        df = pd.DataFrame(self._conn.execute("SELECT bar_open, at, price, base, quote, equity FROM marks "
                                             "ORDER BY at").fetchall(),
                          columns=["bar_open", "at", "price", "base", "quote", "equity"])
        for col in ("price", "base", "quote", "equity"):
            df[col] = df[col].astype(float)
        return df
```

- [ ] **Step 4: Mark in the trader**

In `src/quant_research/paper/trader.py`, add after `ingest`:

```python
    def _mark(self, bar: dict[str, Any], price: Decimal) -> None:
        b = self.balances()
        self.ledger.record_mark(str(bar["bar_open_time"]), _close_epoch(bar), price,
                                b[self.product.base_currency], b[self.product.quote_currency])
```

In `ingest`, before `return True`, add `self._mark(bar, D(bar["close"]))`.

In `on_bar`, after `self.reconcile()` and before `fraction = target_fraction(...)`, add `self._mark(bar, close)`.

- [ ] **Step 5: Report returns from marks, buy and hold with costs**

In `src/quant_research/desk/report.py`, change `from quant_research.paper.fleet import RosterEntry, load_roster` to `from quant_research.paper.fleet import Roster, RosterEntry, entry_costs, load_roster` (`RosterEntry` goes away with the old `parity` in Task 12), and replace the beginning of `paper_section` up to and including the `fees = sum(...)` line with:

```python
def paper_section(paths: Paths, day: str) -> tuple[list[str], dict[str, Any]]:
    t0, t1 = _window(day)
    rp = roster_path(paths)
    roster = load_roster(rp) if rp.exists() else Roster()
    rows, details, summary = [], [], {"strategies": 0, "parity_mismatches": 0, "alerts": 0}
    for entry in sorted(roster.entries, key=lambda e: e.name):
        name = entry.name
        db = paths.paper / name / "paper.db"
        if not db.exists():
            continue
        ledger = Ledger(db)
        try:
            marks = ledger.marks()
            all_dec = ledger.decisions()
            day_dec = [d for d in all_dec if t0 <= d["at"] < t1]
            fills = [f for f in ledger.fills() if t0 <= f.ts < t1]
            par = parity(entry, ledger, day_dec, paths)
        finally:
            ledger.close()
        alerts = Counter()
        ev_path = paths.paper / name / "events.jsonl"
        if ev_path.exists():
            for line in ev_path.read_text().splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if t0 <= ev.get("ts", 0) < t1 and ev.get("level") in ("WARN", "ALERT"):
                    alerts[ev["event"]] += 1
        day_ret = bh_ret = total_ret = bh_total = None
        upto = marks[marks["at"] <= t1]
        end = upto.iloc[-1] if len(upto) and upto.iloc[-1]["at"] > t0 else None
        if end is not None:
            before = marks[marks["at"] <= t0]
            start = before.iloc[-1] if len(before) else marks[marks["at"] > t0].iloc[0]
            fee, slip, _ = entry_costs(entry, roster)
            day_ret = end["equity"] / start["equity"] - 1
            bh_ret = end["price"] / start["price"] - 1
            total_ret = end["equity"] / entry.initial_cash - 1
            p0 = float(all_dec[0]["price"]) if all_dec else marks.iloc[0]["price"]
            bh_total = end["price"] / (p0 * (1 + slip / 10_000) * (1 + fee)) - 1
        fees = sum(float(f.fee) for f in fills)
```

and in the `rows.append(...)` expression replace `float(end['equity'])` with `end['equity']` and the condition `if end else` with `if end is not None else`. Update the table caption line to:

```python
             "Simulated fills at the mid price plus slippage and fees on live Coinbase public data. Returns use "
             "the equity marked at every bar close; total buy & hold includes the entry fee and slippage.", "",
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_paper.py tests/test_report.py tests/test_fleet.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/quant_research/paper/ledger.py src/quant_research/paper/trader.py src/quant_research/desk/report.py tests/test_paper.py tests/test_report.py
git commit -m "paper: mark equity at every bar close; daily returns and buy & hold (with costs) come from the marks"
```

---

### Task 12: Parity on targets, orders and fills from the bar store

**Files:**
- Create: `src/quant_research/paper/parity.py`
- Modify: `src/quant_research/desk/report.py` (use `paper_parity`, delete `parity`, `MAX_PARITY_CHECKS`)
- Modify: `src/quant_research/paper/trader.py` (`warm_up` stops copying the store into the ledger)
- Modify: `src/quant_research/paper/ledger.py` (delete `record_bars`)
- Test: `tests/test_paper.py`, `tests/test_report.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_paper.py`:

```python
def test_parity_is_clean_for_a_replay_with_partial_positions_and_a_band(tmp_path):
    from quant_research.paper.fleet import Roster, RosterEntry
    from quant_research.paper.parity import paper_parity

    bars = _bars(400)
    policy = PortfolioPolicy(vol_target=0.5, vol_lookback=24, band=0.05)
    cfg = _cfg(policy=policy, buy_cash_buffer=0.0)
    trader = run_replay(cfg, bars, tmp_path / "p", PRODUCT)
    entry = RosterEntry(name="t", product="BTC-USD", strategy_ref=cfg.strategy_ref, parameters=cfg.parameters,
                        policy=policy, fee_rate=0.001, slippage_bps=2.0)
    out = paper_parity(entry, Roster(), trader.ledger, bars, None)
    assert out["checked"] > 100 and out["skipped"] == 0
    assert out["target_mismatches"] == [] and out["order_mismatches"] == []
    assert out["fills"] > 4 and abs(out["fill_vs_next_open_bps"]) < 1e-6 and out["fee_rate_error"] < 1e-9
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_paper.py::test_parity_is_clean_for_a_replay_with_partial_positions_and_a_band -q`
Expected: FAIL (`ModuleNotFoundError: quant_research.paper.parity`).

- [ ] **Step 3: Create the parity module**

`src/quant_research/paper/parity.py`:

```python
"""Backtest-vs-paper parity on the bars a paper trader saw.

Targets are recomputed from the bar store with the research code. Orders are re-derived decision by decision with
the backtest's rebalancing rule from the trader's own state at that bar, so one difference does not cascade. Fills
are compared with the backtest's fill model: the next bar's open plus slippage, and the configured fee rate.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from quant_research.backtest.rebalance import needs_trade
from quant_research.data.bars import to_epoch
from quant_research.paper.fleet import Roster, RosterEntry, entry_costs
from quant_research.paper.ledger import Ledger
from quant_research.strategy.compute import compute_targets, strategy_weights
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.strategy.spec import StrategySpec
from quant_research.timeutil import granularity_seconds, periods_per_year


def _epoch(value: Any) -> int:
    return int(to_epoch(pd.Series([pd.Timestamp(value)]))[0])


def _fills_by_order(ledger: Ledger) -> dict[str, tuple[float, float, float]]:
    """client_order_id -> (quantity, notional, fee) summed over its fills."""
    out: dict[str, tuple[float, float, float]] = {}
    for ev in ledger.fills():
        q, n, f = out.get(ev.client_order_id, (0.0, 0.0, 0.0))
        out[ev.client_order_id] = (q + float(ev.fill_qty), n + float(ev.fill_qty * ev.fill_price), f + float(ev.fee))
    return out


def paper_parity(entry: RosterEntry, roster: Roster, ledger: Ledger, bars: pd.DataFrame, paths,
                 since: float = -math.inf, until: float = math.inf) -> dict[str, Any]:
    out: dict[str, Any] = {"checked": 0, "skipped": 0, "target_mismatches": [], "order_mismatches": [],
                           "fills": 0, "fill_vs_next_open_bps": None, "fee_rate_error": None}
    decisions = [d for d in ledger.decisions() if since <= d["at"] < until and d.get("history_start")]
    if not decisions:
        return out | {"note": "no decisions in the window"}
    if not len(bars):
        return out | {"skipped": len(decisions), "note": "no bar store"}
    opens = to_epoch(bars["bar_open_time"])
    checkable = [d for d in decisions if _epoch(d["history_start"]) == int(opens[0])]
    out["skipped"] = len(decisions) - len(checkable)
    if not checkable:
        return out | {"note": "decisions predate the bar store"}
    upto = bars[opens <= _epoch(checkable[-1]["bar_open"])].reset_index(drop=True)
    spec = StrategySpec(strategy_ref=entry.strategy_ref, parameters=entry.parameters)
    sandbox = SandboxConfig(isolation="docker" if entry.strategy_ref.startswith("plugin:") else "subprocess")
    try:
        tr = compute_targets(spec, upto, paths, sandbox)
    except Exception as exc:  # noqa: BLE001
        return out | {"checked": len(checkable),
                      "target_mismatches": [f"recompute failed ({type(exc).__name__}: {exc})"]}
    weights, _ = strategy_weights(tr, entry.policy, upto, granularity_seconds(entry.granularity),
                                  periods_per_year(entry.granularity))
    index = {int(t): i for i, t in enumerate(opens)}
    marks = ledger.marks()
    mark_at = {_epoch(r["bar_open"]): r for r in marks.to_dict("records")}
    fills = _fills_by_order(ledger)
    fee_rate, slip_bps, min_notional = entry_costs(entry, roster)
    next_open = bars["open"].to_numpy(float)
    slips, fee_errors = [], []
    for d in checkable:
        t = _epoch(d["bar_open"])
        i = index[t]
        recomputed = None if np.isnan(weights[i]) else float(weights[i])
        recorded = d["target"]["target_fraction"]
        if recomputed != recorded:
            out["target_mismatches"].append(f"{d['bar_open']}: recorded {recorded}, recomputed {recomputed}")
        paper_side = d["intent"]["side"] if d.get("intent") else None
        rule_side = None
        m = mark_at.get(t)
        if d["decision"]["approved"] and m is not None:
            target = d["decision"]["target_fraction"]
            position = m["base"] * m["price"]
            if needs_trade(target, position, m["equity"], tolerance=entry.policy.band, min_notional=min_notional):
                rule_side = "buy" if target * m["equity"] > position else "sell"
        if paper_side != rule_side:
            out["order_mismatches"].append(f"{d['bar_open']}: paper {paper_side or 'no order'}, backtest rule "
                                           f"{rule_side or 'no order'}")
        cid = d["intent"]["client_order_id"] if d.get("intent") else None
        if cid in fills and i + 1 < len(next_open):
            qty, notional, fee = fills[cid]
            sign = 1 if paper_side == "buy" else -1
            ref = next_open[i + 1] * (1 + sign * slip_bps / 10_000)
            slips.append((notional / qty / ref - 1) * 10_000 * sign)
            fee_errors.append(abs(fee / notional - fee_rate))
    out["checked"] = len(checkable)
    out["fills"] = len(slips)
    if slips:
        out["fill_vs_next_open_bps"] = float(np.mean(slips))
        out["fee_rate_error"] = float(max(fee_errors))
    return out
```

- [ ] **Step 4: Run it**

Run: `uv run pytest tests/test_paper.py::test_parity_is_clean_for_a_replay_with_partial_positions_and_a_band -q`
Expected: PASS.

- [ ] **Step 5: Use it in the report**

In `src/quant_research/desk/report.py`:
- Delete `MAX_PARITY_CHECKS` and the function `parity`; remove the now unused imports `RosterEntry`, `target_fraction`, `SandboxConfig`, `StrategySpec` (keep `pd` if still used, otherwise remove it too; `uv run ruff check .` tells).
- Add imports `from quant_research.paper.barstore import BarStore` and `from quant_research.paper.parity import paper_parity`.
- In `paper_section`, before the `for entry in ...` loop add `stores: dict[tuple[str, str], Any] = {}`; inside the loop, before `ledger = Ledger(db)`, add:

```python
        key = (entry.product, entry.granularity)
        if key not in stores:
            stores[key] = BarStore.for_feed(paths, *key).bars
```

- Replace `par = parity(entry, ledger, day_dec, paths)` with `par = paper_parity(entry, roster, ledger, stores[key], paths, since=t0, until=t1)`.
- Replace the summary and cell computations that use `par["mismatches"]`:

```python
        mismatches = par["target_mismatches"] + par["order_mismatches"]
        summary["parity_mismatches"] += len(mismatches)
```

and in the rows use `{par['checked'] - len(mismatches)}/{par['checked']}` (with-equity row) and `{par['checked']}` (no-equity row) as before; in `details` use `mismatches` in place of `par["mismatches"]`, and append the execution line when there were fills:

```python
        if alerts or mismatches or not entry.enabled:
            details.append(f"- **{name}**{' (disabled)' if not entry.enabled else ''}: "
                           + ", ".join(f"{k}×{v}" for k, v in alerts.items())
                           + "".join(f"\n  - parity: {m}" for m in mismatches[:5]))
        if par["fills"]:
            details.append(f"- {name} execution: {par['fills']} fills, {par['fill_vs_next_open_bps']:+.1f} bps vs "
                           f"the next bar's open, fee rate error {par['fee_rate_error']:.2e}"
                           + (f"; {par['skipped']} decisions predate the bar store" if par["skipped"] else ""))
```

- Update the module docstring's parity sentence to: `decision parity (targets recomputed from the bar store, orders re-derived with the backtest rule, fills compared with the next bar's open)`.

- [ ] **Step 6: Stop copying the store into each ledger**

In `src/quant_research/paper/trader.py`, in `warm_up`, delete the line `self.ledger.record_bars(new)`. In `src/quant_research/paper/ledger.py`, delete the method `record_bars`.

- [ ] **Step 7: Run the paper, fleet and report tests**

Run: `uv run pytest tests/test_paper.py tests/test_fleet.py tests/test_report.py tests/test_paper_data.py -q`
Expected: all pass. `test_daily_report_from_fixed_data` still shows `0 parity mismatches` and `6/6 |`, and the tampered decision gives `recorded 0.5`.

- [ ] **Step 8: Commit**

```bash
git add src/quant_research/paper/parity.py src/quant_research/desk/report.py src/quant_research/paper/trader.py src/quant_research/paper/ledger.py tests/test_paper.py
git commit -m "report: parity on targets, orders and fills recomputed from the bar store"
```

---

### Task 13: Documentation, full verification, release

**Files:**
- Modify: `docs/development.md`

- [ ] **Step 1: Document the paper changes**

In `docs/development.md`, in the paper/fleet part of the architecture section, add a paragraph (adapt headings to the file):

```markdown
Paper history is a bar store per product and granularity (`paper/bars/<product>-<granularity>.parquet`),
anchored at the first bar of the frozen dataset and only appended. Strategies compute from the whole store, so a
restart reproduces stateful positions. The poller backfills from the last stored bar after an outage; catch-up bars
rebuild state and only the latest bar can trade. Staleness is the age of the latest bar close. Risk limits edited in
the roster apply to running traders; edits to strategy, parameters, policy, capital or costs are refused (add the
changed strategy under a new name). Failed attaches retry with backoff. Every trader marks equity at each bar close;
the daily report computes returns from the marks and checks parity on targets, orders and fills.

Promotion copies the validated capital and costs into the roster entry, refuses engine settings paper cannot
reproduce (`execution_delay_bars`, `adverse_fill`, `rebalance_tolerance`), and resumes from any intermediate state.
Every holdout path goes through `run_holdout` with the split's viewing limit; a repeat of an already viewed
candidate is not a new viewing; `zqr holdout-test --override-reason` exceeds the limit and marks the split's
holdout as contaminated.
```

In the limitations section, remove any statement these changes make false, and note that
`docs/reviews/2026-10-13/reproduce_findings.py` asserts the defects as they were at `a28d9b2`; the corresponding
correct-behaviour tests live in `tests/`.

- [ ] **Step 2: Full suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass (121 + new), ruff clean.

- [ ] **Step 3: Commit**

```bash
git add docs/development.md
git commit -m "docs: paper bar store, freshness, config reload, promotion and holdout gate"
```

- [ ] **Step 4: Local rehearsal**

Run: `uv run zqr release --target local` (builds the image and starts it as `zqr-desk` on this Mac; see `docs/development.md`); check `zqr status` shows the fleet running, `paper/bars/BTC-USD-ONE_HOUR.parquet` exists in the volume with rows from 2021-01-01, and `fleet_health.json` has no `stale` strategy. Stop with `zqr down`.

- [ ] **Step 5: Push and release to production (ask the user first; both need approval)**

```bash
git push origin main
uv run zqr release
```

Expected on the first start: the fleet seeds both stores from the frozen datasets (50,670 rows each) plus the bars since, every trader warms up from 2021, and breakout traders whose full-history position differs from their old 3,000-bar position place one corrective order. Check `zqr status` and `fleet.jsonl` (`caught_up`, `bar_gap`, `strategy_attached`).

- [ ] **Step 6: Human checkpoint**

After the next UTC day completes, open that day's report (`desk/reports/<day>.md`) and give the user: parity per strategy (target and order mismatches, decisions skipped because they predate the store), the execution lines (fill bps, fee error), stale or gap alerts. The design's phase 0 checkpoint is the user's review of this report.
```
