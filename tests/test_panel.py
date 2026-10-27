from quant_research.desk.panel import PANEL_HTML, book_role, snapshot
from quant_research.desk.presets import preset_roster
from quant_research.paper.fleet import RosterEntry
from tests.test_portfolio_report import _paper_portfolios


def test_panel_snapshot_reads_paper_books(tmp_path):
    paths, store = _paper_portfolios(tmp_path)
    snap = snapshot(paths)
    assert snap["sleeves_n"] == 1 and snap["books"]
    book = next(b for b in snap["books"] if b["name"] == "portfolio-btc-usd")
    assert book["equity_series"] and book["bh_series"] and book["role"] == "research"
    assert book["equity"] > 0 and "ret" in book
    assert snap["research_pnl"] == snap["sleeves_pnl"]
    assert PANEL_HTML.is_file() and "research P&L" in PANEL_HTML.read_text()
    store.close()


def test_panel_combines_official_research_books(tmp_path):
    paths, store = _paper_portfolios(tmp_path, ("BTC-USD", "ETH-USD"))
    snap = snapshot(paths)
    head = snap["books"][0]
    assert head["role"] == "combined" and head["name"] == "research combined"
    assert set(head["combined_of"]) == {"portfolio-btc-usd", "portfolio-eth-usd"}
    individuals = [b for b in snap["books"] if b["role"] == "research"]
    assert snap["research_n"] == 2
    assert abs(head["equity"] - sum(b["equity"] for b in individuals)) < 1e-6
    store.close()


def test_book_role_splits_presets_from_research():
    research = RosterEntry(name="shadow-btc-usd-admitted-db7da5b1", product="BTC-USD",
                           strategy_ref="portfolio:pv-x", source="portfolio", portfolio_version="pv-x")
    shadow = research.model_copy(update={"shadow": True, "name": "shadow-btc-usd"})
    cash = RosterEntry(name="btc-cash", product="BTC-USD", strategy_ref="template:cash", source="preset")
    mom = RosterEntry(name="btc-mom24", product="BTC-USD", strategy_ref="template:momentum", source="preset")
    assert book_role(research) == "research"
    assert book_role(shadow) == "shadow"
    assert book_role(cash) == "thermometer"
    assert book_role(mom) == "baseline"


def test_preset_roster_uses_authoritative_costs():
    r = preset_roster()
    assert r.fee_rate == 0.002 and r.slippage_bps == 2.0
    assert all(e.fee_rate == 0.002 and e.slippage_bps == 2.0 for e in r.entries)
