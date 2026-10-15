"""Portfolio sections of the desk reports, the monthly review and the soft stop (design 5.9, investment policy 2)."""

import math
from decimal import Decimal

import numpy as np
import pytest

from quant_research.desk import softstop
from quant_research.desk.config import DeskConfig
from quant_research.desk.portfolio_report import combined, family_contribution, load_books, write_monthly_report
from quant_research.desk.promotion import roster_path
from quant_research.desk.report import research_section, write_daily_report
from quant_research.experiments.store import ExperimentStore
from quant_research.paper.barstore import BarStore
from quant_research.paper.fleet import Roster, RosterEntry, save_roster
from quant_research.paper.ledger import Ledger
from quant_research.paper.runner import run_replay
from quant_research.paths import get_paths
from quant_research.portfolio.version import VersionStore
from tests.conftest import HOUR, T0
from tests.test_paper import PRODUCT
from tests.test_portfolio_paper import OVERLAY, _bars, _cfg, _version

DAY = 86_400


def _entry(vid, product="BTC-USD", initial=10_000.0, fee=0.001, slip=2.0):
    return RosterEntry(name=f"portfolio-{product.lower()}", product=product, strategy_ref=f"portfolio:{vid}",
                       parameters={"version_id": vid}, source="portfolio", portfolio_version=vid,
                       initial_cash=initial, fee_rate=fee, slippage_bps=slip)


def _paper_portfolios(tmp_path, products=("BTC-USD",)):
    paths = get_paths(tmp_path / "home")
    store = ExperimentStore(paths.db)
    entries = []
    for k, product in enumerate(products):
        v = _version(overlay=OVERLAY, product=product)
        vid = VersionStore(store).put(v)
        bars = _bars(seed=3 + k)
        BarStore.for_feed(paths, product, "ONE_HOUR").append(bars)
        e = _entry(vid, product)
        entries.append(e)
        run_replay(_cfg(v, name=e.name, instrument=product), bars, paths.paper / e.name,
                   PRODUCT).ledger.close()
    save_roster(roster_path(paths), Roster(fee_rate=0.001, slippage_bps=2, entries=entries))
    return paths, store


def test_report_has_a_portfolio_section(tmp_path):
    paths, store = _paper_portfolios(tmp_path)
    text = write_daily_report(paths, store, "2024-01-05").read_text()
    assert "## Portfolios" in text and "| portfolio-btc-usd | pv-" in text
    row = next(line for line in text.splitlines() if line.startswith("| portfolio-btc-usd |"))
    assert "| draft |" in row and "| full" in row and "24/24 |" in row
    assert "Summary: 1 paper portfolios, 0 paper strategies" in text and "0 parity mismatches" in text
    assert "families hold" in text and "day P&L by family (USD)" in text and "Combined account" in text
    assert "(no paper strategies with a ledger)" in text
    store.close()


def test_combined_account_is_the_sum_of_the_two_portfolios(tmp_path):
    paths, store = _paper_portfolios(tmp_path, ("BTC-USD", "ETH-USD"))
    books = load_books(paths)
    comb = combined(books)
    assert len(books) == 2 and len(comb) == 400
    for b in books:
        m = b.marks.set_index("at")
        assert comb[f"equity:{b.entry.name}"].equals(m["equity"].loc[comb.index])
        p0 = float(b.decisions[0]["price"])
        bench = 10_000 * m["price"] / (p0 * (1 + 2 / 10_000) * 1.001)
        assert np.allclose(comb[f"benchmark:{b.entry.name}"], bench.loc[comb.index])
    assert np.allclose(comb["combined"], comb.filter(like="equity:").sum(axis=1))
    text = write_daily_report(paths, store, "2024-01-05").read_text()
    assert "Combined account: equity" in text and "BTC-USD" in text and "ETH-USD" in text
    store.close()


def test_family_contribution_splits_daily_pnl_by_target_share(tmp_path):
    paths, store = _paper_portfolios(tmp_path)
    book = load_books(paths)[0]
    t0 = T0 + 4 * DAY
    contrib = family_contribution(book, t0, t0 + DAY)
    eq = book.marks.set_index("at")["equity"]
    pnl = eq[eq.index <= t0 + DAY].iloc[-1] - eq[eq.index <= t0].iloc[-1]
    assert set(contrib) <= {"trend", "hold", "none"}
    assert sum(contrib.values()) == pytest.approx(pnl, abs=0.02)
    store.close()


def test_research_section_shows_versions_and_validation_uses(paths, store, dataset, monkeypatch):
    from quant_research.portfolio.evaluate import validate
    from quant_research.portfolio.seed import define_seed
    from tests.test_portfolio import _desk, _lenient

    _desk(paths, store, dataset)
    _lenient(monkeypatch)
    validate(paths, store, define_seed(paths, store, "BTC-USD"))
    text = "\n".join(research_section(paths, store, "2024-01-05")[0])
    assert "| product | portfolio versions by state | validation uses |" in text
    assert "| BTC-USD | validated 1 | 1 |" in text


def test_monthly_report_on_the_last_day(tmp_path):
    from quant_research.desk.commands import _report

    paths, store = _paper_portfolios(tmp_path)
    out = write_monthly_report(paths, store, "2024-01")
    text = out.read_text()
    assert out.name == "monthly-2024-01.md" and "# Monthly review 2024-01" in text
    assert "| portfolio-btc-usd | pv-" in text and "Combined account: month" in text
    assert "Soft stop (investment policy 2): too early" in text
    store.close()
    out.unlink()
    _report(paths, "2024-01-15")
    assert not out.exists()
    _report(paths, "2024-01-31")
    assert out.exists()


# -- soft stop -------------------------------------------------------------------------------------------------

def _marks(tmp_path, days, shift):
    """Daily marks of one portfolio whose daily returns are buy and hold's minus `shift`."""
    paths = get_paths(tmp_path / "home")
    e = _entry("pv-x", fee=0.0, slip=0.0)
    save_roster(roster_path(paths), Roster(entries=[e]))
    rng = np.random.default_rng(7)
    r = rng.normal(0.001, 0.02, days)
    price = 100 * np.cumprod(1 + r)
    equity = 10_000 * np.cumprod(1 + r - shift)
    ledger = Ledger(paths.paper / e.name / "paper.db")
    for k in range(days):
        at = T0 + k * DAY + 23 * HOUR
        ledger.record_mark(f"day-{k}", at, Decimal(str(price[k])), Decimal(0), Decimal(str(equity[k])))
    ledger.close()
    return paths, r


def _shift_for(r, difference):
    window = r[-softstop.WINDOW_DAYS:]
    return -difference * window.std(ddof=1) / math.sqrt(365)


def _review_day(days):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(T0 + days * DAY, UTC).strftime("%Y-%m-%d")


def test_soft_stop_waits_for_twelve_months(tmp_path):
    paths, _ = _marks(tmp_path, 330, 0.01)
    out = softstop.evaluate(paths, _review_day(330))
    assert out["status"] == "too early" and not softstop.soft_stop_path(paths).exists()


def test_soft_stop_triggers_below_minus_half(tmp_path, store):
    from quant_research.desk import research as rl

    _, r = _marks(tmp_path / "probe", 400, 0.0)
    paths, _ = _marks(tmp_path / "a", 400, _shift_for(r, -0.4))
    out = softstop.evaluate(paths, _review_day(400))
    assert out["status"] == "ok" and out["difference"] == pytest.approx(-0.4, abs=0.01)
    paths, _ = _marks(tmp_path / "b", 400, _shift_for(r, -0.6))
    out = softstop.evaluate(paths, _review_day(400))
    assert out["status"] == "triggered" and out["difference"] == pytest.approx(-0.6, abs=0.01)
    assert softstop.active(paths)["benchmark_sharpe"] == out["benchmark_sharpe"] and out["window"]
    assert softstop.evaluate(paths, _review_day(400))["status"] == "active"
    cfg = DeskConfig(research_enabled=True, mandates={"BTC-USD": "m"})
    loop = rl.ResearchLoop(paths, ExperimentStore(paths.db), cfg, adapter=None, require_check=False)
    step = loop.step()
    assert step["status"] == "paused" and "soft stop" in step["reason"]


def test_clear_needs_a_note_and_is_logged(tmp_path):
    _, r = _marks(tmp_path / "probe", 400, 0.0)
    paths, _ = _marks(tmp_path / "b", 400, _shift_for(r, -0.6))
    softstop.evaluate(paths, _review_day(400))
    with pytest.raises(ValueError, match="note"):
        softstop.clear(paths, " ")
    rec = softstop.clear(paths, "reviewed with the human; keep researching")
    assert rec["note"].startswith("reviewed") and softstop.active(paths) is None
    assert len(list(paths.desk.glob("soft_stop.cleared-*.json"))) == 1
    with pytest.raises(ValueError, match="no soft stop"):
        softstop.clear(paths, "again")
