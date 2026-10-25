from quant_research.desk.panel import PANEL_HTML, snapshot
from tests.test_portfolio_report import _paper_portfolios


def test_panel_snapshot_reads_paper_books(tmp_path):
    paths, store = _paper_portfolios(tmp_path)
    snap = snapshot(paths)
    assert snap["sleeves_n"] == 1 and snap["books"]
    book = snap["books"][0]
    assert book["name"] == "portfolio-btc-usd" and book["equity_series"] and book["bh_series"]
    assert book["equity"] > 0 and "ret" in book
    assert PANEL_HTML.is_file() and "ZQR tape" in PANEL_HTML.read_text()
    store.close()
