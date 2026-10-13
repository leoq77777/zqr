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
