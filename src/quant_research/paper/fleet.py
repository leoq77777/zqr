"""Many paper strategies in one process: one market feed per (product, granularity), one trader per roster entry.

The roster (`desk/roster.json`) is re-read every loop, so promoted candidates join without a restart. Each
strategy keeps its own ledger, events and health under `paper/<name>/`; an error in one strategy is logged and
does not stop the others.
"""

from __future__ import annotations

import json
import logging
import re
import signal
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field, field_validator

from quant_research.data.bars import to_epoch
from quant_research.paper.barstore import BarStore, snapshot_seed
from quant_research.paper.feeds import SEED_BARS, CoinbaseCandlePoller, coinbase_mid_price
from quant_research.paper.models import ProductSpec
from quant_research.paper.risk import RiskLimits
from quant_research.paper.runner import make_trader
from quant_research.paper.trader import PaperConfig, PaperTrader
from quant_research.portfolio.policy import PortfolioPolicy
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.timeutil import parse_granularity

log = logging.getLogger(__name__)


class RosterEntry(BaseModel):
    name: str
    product: str
    granularity: str = "ONE_HOUR"
    strategy_ref: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    policy: PortfolioPolicy = Field(default_factory=PortfolioPolicy)
    initial_cash: float = 10_000.0
    fee_rate: float | None = None  # None = the roster's default
    slippage_bps: float | None = None
    min_trade_notional: float | None = None
    source: Literal["preset", "candidate", "portfolio"] = "preset"
    candidate_id: str | None = None
    portfolio_version: str | None = None  # source "portfolio": the version traded, `strategy_ref` portfolio:<id>
    shadow: bool = False  # source "portfolio": a version not approved for paper, run for forward evidence only
    enabled: bool = True
    added_at: float | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", v):
            raise ValueError("roster names use letters, digits, '_', '.', '-'")
        return v

    @field_validator("granularity")
    @classmethod
    def _gran(cls, v: str) -> str:
        return parse_granularity(v)


class Roster(BaseModel):
    fee_rate: float = 0.006
    slippage_bps: float = 5.0
    risk: RiskLimits = Field(default_factory=RiskLimits)
    entries: list[RosterEntry] = Field(default_factory=list)

    def enabled(self) -> dict[str, RosterEntry]:
        return {e.name: e for e in self.entries if e.enabled}


def entry_costs(e: RosterEntry, roster: Roster) -> tuple[float, float, float]:
    """(fee rate, slippage bps, minimum trade notional) for an entry: its own values, else the roster's."""
    return (roster.fee_rate if e.fee_rate is None else e.fee_rate,
            roster.slippage_bps if e.slippage_bps is None else e.slippage_bps,
            1.0 if e.min_trade_notional is None else e.min_trade_notional)


def load_roster(path: Path) -> Roster:
    return Roster.model_validate_json(Path(path).read_text())


def save_roster(path: Path, roster: Roster) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(roster.model_dump_json(indent=2))
    tmp.replace(path)


def add_entry(path: Path, entry: RosterEntry) -> bool:
    """Append an entry unless its name exists; returns whether it was added."""
    roster = load_roster(path) if path.exists() else Roster()
    if any(e.name == entry.name for e in roster.entries):
        return False
    roster.entries.append(entry.model_copy(update={"added_at": entry.added_at or time.time()}))
    save_roster(path, roster)
    return True


REFUSED = "config change refused"


class ConfigChangeRefused(RuntimeError):
    pass


def _refusal(held_by: str, running: str, roster: str) -> str:
    return f"{REFUSED}: {held_by} {running}, roster {roster}; add the changed strategy under a new name"


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


class PaperFleet:
    def __init__(self, paths, roster_path: Path, client, *, clock: Callable[[], float] = time.time,
                 on_activate: Callable[[RosterEntry], None] | None = None, seed_bars: int = SEED_BARS):
        self.paths, self.roster_path, self.client, self.clock = paths, Path(roster_path), client, clock
        self.on_activate = on_activate
        self.seed_bars = seed_bars
        self.feeds: dict[tuple[str, str], _Feed] = {}
        self.traders: dict[str, PaperTrader] = {}
        self.entries: dict[str, RosterEntry] = {}
        self.products: dict[str, ProductSpec] = {}
        self.errors: dict[str, str] = {}
        self.refused: dict[str, dict[str, str]] = {}  # name -> running and roster config hashes
        self._retry_at: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._roster_mtime: float | None = None
        self.roster = Roster()
        self.workdir = paths.paper
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._versions: dict[str, dict[str, Any]] = {}

    # -- logging ---------------------------------------------------------------------------------
    def _log(self, level: str, event: str, **data: Any) -> None:
        with open(self.workdir / "fleet.jsonl", "a") as f:
            f.write(json.dumps({"ts": self.clock(), "level": level, "event": event, **data}, default=str) + "\n")
        getattr(log, "warning" if level in ("WARN", "ALERT") else "info")("%s %s", event, data)

    # -- roster ----------------------------------------------------------------------------------
    def _product(self, product_id: str) -> ProductSpec:
        if product_id not in self.products:
            self.products[product_id] = ProductSpec.from_coinbase(self.client.product(product_id).body)
        return self.products[product_id]

    def _version_body(self, vid: str) -> dict[str, Any]:
        """A portfolio version's body (immutable, so read once)."""
        if vid not in self._versions:
            from quant_research.experiments.store import ExperimentStore
            from quant_research.portfolio.version import VersionStore

            store = ExperimentStore(self.paths.db)
            try:
                self._versions[vid] = VersionStore(store).version(vid).model_dump(mode="json")
            finally:
                store.close()
        return self._versions[vid]

    def _config(self, e: RosterEntry) -> PaperConfig:
        r = self.roster
        fee, slip, min_notional = entry_costs(e, r)
        sandbox = SandboxConfig(isolation="docker" if e.strategy_ref.startswith("plugin:") else "subprocess")
        risk = {"kill_switch_file": str(self.workdir / e.name / "KILL")}
        portfolio = None
        if e.source == "portfolio":
            if not e.portfolio_version or e.strategy_ref != f"portfolio:{e.portfolio_version}":
                raise ValueError(f"{e.name}: a portfolio entry needs portfolio_version and strategy_ref "
                                 "portfolio:<version>")
            portfolio = self._version_body(e.portfolio_version)
            risk["max_order_notional"] = e.initial_cash  # investment policy D1: the order cap is the capital
        return PaperConfig(name=e.name, instrument=e.product, granularity=e.granularity, strategy_ref=e.strategy_ref,
                           parameters=e.parameters, policy=e.policy, initial_quote=Decimal(str(e.initial_cash)),
                           fee_rate=Decimal(str(fee)), slippage_bps=Decimal(str(slip)),
                           min_trade_notional=min_notional, risk=r.risk.model_copy(update=risk), sandbox=sandbox,
                           portfolio=portfolio)

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
            self.errors.pop(name, None)
            self.refused.pop(name, None)
            self._log("INFO", "strategy_detached", name=name)
        for name in [n for n in self._retry_at if n not in wanted]:
            self._retry_at.pop(name)
            self._failures.pop(name, None)
            self.errors.pop(name, None)
            self.refused.pop(name, None)
        for name, e in wanted.items():
            if name in self.traders:
                self._refresh(name, e)

    def _attach_missing(self) -> None:
        now = self.clock()
        for name, e in self.roster.enabled().items():
            if name in self.traders or now < self._retry_at.get(name, 0.0):
                continue
            trader = None
            try:
                feed = self.feeds.get((e.product, e.granularity))
                if feed is None:
                    feed = self.feeds[(e.product, e.granularity)] = _Feed(self.paths, self.client, e.product,
                                                                         e.granularity, self.clock, self.seed_bars)
                trader = make_trader(self._config(e), self.workdir / name, self._product(e.product),
                                     paths=self.paths, clock=self.clock)
                stored, want = trader.ledger.get("config_hash"), trader.cfg.identity_hash()
                if stored is None:
                    trader.ledger.put("config_hash", want)
                elif stored != want:
                    self.refused[name] = {"running": stored, "roster": want}
                    raise ConfigChangeRefused(_refusal("ledger", stored, want))
                trader.recover()
                history, last = feed.history, trader.ledger.get("last_bar_open")
                if last is not None:
                    cutoff = to_epoch(pd.to_datetime(pd.Series([last]), utc=True))[0]
                    history = history[to_epoch(history["bar_open_time"]) <= cutoff]
                trader.warm_up(history)
            except Exception as exc:  # noqa: BLE001
                if trader is not None:
                    trader.ledger.close()
                n = self._failures[name] = self._failures.get(name, 0) + 1
                self._retry_at[name] = now + min(60.0 * 2 ** (n - 1), 1800.0)
                self.errors[name] = str(exc) if isinstance(exc, ConfigChangeRefused) else f"{type(exc).__name__}: {exc}"
                self._log("ALERT", "strategy_attach_failed", name=name, error=self.errors[name], attempt=n)
                continue
            self.traders[name], self.entries[name] = trader, e
            self.errors.pop(name, None)
            self.refused.pop(name, None)
            self._retry_at.pop(name, None)
            self._failures.pop(name, None)
            self._log("INFO", "strategy_attached", name=name, strategy=e.strategy_ref, product=e.product)
            if self.on_activate is not None:
                try:
                    self.on_activate(e)
                except Exception as exc:  # noqa: BLE001
                    self._log("WARN", "activate_hook_failed", name=name, error=f"{type(exc).__name__}: {exc}")

    def _refresh(self, name: str, e: RosterEntry) -> None:
        """Risk limits update in place; anything else that changes what is traded is refused."""
        trader, want = self.traders[name], self._config(e)
        running = trader.cfg.identity_hash()
        if want.identity_hash() != running:
            if not self.errors.get(name, "").startswith(REFUSED):
                self.errors[name] = _refusal("running", running, want.identity_hash())
                self.refused[name] = {"running": running, "roster": want.identity_hash()}
                self._log("ALERT", "config_change_refused", name=name, running=running,
                          wanted=want.identity_hash())
            return
        if self.errors.get(name, "").startswith(REFUSED):
            self.errors.pop(name)
        self.refused.pop(name, None)
        self.entries[name] = e
        if want.risk != trader.cfg.risk:
            trader.update_risk(want.risk)

    # -- loop ------------------------------------------------------------------------------------
    def step(self) -> dict[str, Any]:
        self.sync_roster()
        for (product, gran), feed in self.feeds.items():
            members = [n for n, e in self.entries.items() if (e.product, e.granularity) == (product, gran)]
            try:
                bars = feed.poll()
            except Exception as exc:  # noqa: BLE001 - staleness shows in health; keep looping
                self._log("ALERT", "feed_error", product=product, error=f"{type(exc).__name__}: {exc}")
                continue
            if len(bars) > 1:
                self._log("WARN", "caught_up", product=product, bars=len(bars))
            missing = len(feed.store.missing_opens()) if bars else feed.missing
            if missing > feed.missing:
                self._log("ALERT", "bar_gap", product=product, missing=missing - feed.missing)
                feed.missing = missing
            for name in members:
                trader = self.traders[name]
                try:
                    self._catch_up(trader, feed)
                except Exception as exc:  # noqa: BLE001
                    self.errors[name] = f"{type(exc).__name__}: {exc}"
                    trader._log("ALERT", "bar_error", error=self.errors[name])
                    continue
                if name in self.errors and not self.errors[name].startswith(REFUSED):
                    del self.errors[name]
            pending = [n for n in members if self.traders[n].live_orders()]
            if pending:
                try:
                    price = coinbase_mid_price(self.client, product)
                except Exception as exc:  # noqa: BLE001
                    self._log("ALERT", "price_error", product=product, error=f"{type(exc).__name__}: {exc}")
                    continue
                for name in pending:
                    try:
                        self.traders[name].process_market(price, self.clock())
                    except Exception as exc:  # noqa: BLE001
                        self.errors[name] = f"{type(exc).__name__}: {exc}"
                        self.traders[name]._log("ALERT", "market_error", error=self.errors[name])
        return self.write_health()

    @staticmethod
    def _catch_up(trader: PaperTrader, feed: _Feed) -> None:
        """Deliver the stored bars the trader has not seen: all but the latest without deciding, then the latest."""
        bars = feed.history
        if trader.history.empty:
            trader.warm_up(bars.iloc[:-1])
            bars = bars.iloc[-1:]
        elif len(bars):
            last = to_epoch(trader.history["bar_open_time"])[-1]
            bars = bars[to_epoch(bars["bar_open_time"]) > last]
        rows = bars.to_dict("records")
        for bar in rows[:-1]:
            trader.ingest(bar)
        if rows:
            trader.on_bar(rows[-1])

    def write_health(self) -> dict[str, Any]:
        strategies = {}
        for name, trader in self.traders.items():
            try:
                strategies[name] = trader.write_health()
            except Exception as exc:  # noqa: BLE001
                strategies[name] = {"error": f"{type(exc).__name__}: {exc}"}
        health = {"ts": self.clock(), "strategies": strategies, "errors": dict(self.errors),
                  "config_refused": dict(self.refused), "feeds": [f"{p}:{g}" for p, g in self.feeds]}
        (self.workdir / "fleet_health.json").write_text(json.dumps(health, indent=2, default=str))
        return health

    def run(self, poll_seconds: float = 30.0, max_iterations: int | None = None,
            sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
        """Loop until SIGTERM/SIGINT or `max_iterations`; returns the final health, written before the ledgers
        close."""
        stop = {"flag": False}

        def _stop(*_):
            stop["flag"] = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        self._log("INFO", "fleet_started", roster=str(self.roster_path))
        i = 0
        while not stop["flag"] and (max_iterations is None or i < max_iterations):
            i += 1
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001
                self._log("ALERT", "loop_error", error=f"{type(exc).__name__}: {exc}")
            waited = 0.0
            while (max_iterations is None or i < max_iterations) and not stop["flag"] and waited < poll_seconds:
                sleep(min(1.0, poll_seconds - waited))
                waited += 1.0
        health = self.write_health()
        for trader in self.traders.values():
            trader._log("INFO", "stopped")
            trader.ledger.close()
        self._log("INFO", "fleet_stopped")
        return health
