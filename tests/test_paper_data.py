from quant_research.data.bars import to_epoch
from quant_research.data.snapshot import freeze_dataset
from quant_research.data.validate import validate_bars
from quant_research.paper.barstore import BarStore, snapshot_seed
from quant_research.paper.feeds import CoinbaseCandlePoller
from quant_research.paper.fleet import _Feed
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


def test_feed_is_anchored_at_the_frozen_dataset(paths, dataset):
    clock = {"now": float(NOW0)}
    feed = _Feed(paths, FakeCoinbase(lambda: clock["now"]), "BTC-USD", "ONE_HOUR", lambda: clock["now"],
                 seed_bars=10)
    opens = to_epoch(feed.history["bar_open_time"])
    assert opens[0] == T0
    assert opens[-1] == NOW0 - NOW0 % HOUR - HOUR
    assert (feed.store.missing_opens() == [])


def test_seed_is_the_longest_coinbase_dataset(paths):
    def freeze(venue, n, start, **extra):
        bars, rep = validate_bars(make_raw(n, start=start), venue, "BTC-USD", "ONE_HOUR", start, start + n * HOUR)
        freeze_dataset(paths, bars, rep, extra=extra)

    freeze("coinbase", 2000, T0)
    freeze("coinbase", 100, T0 + 2500 * HOUR)
    freeze("lab", 3000, T0 - 1000 * HOUR, synthetic=True)
    seed = snapshot_seed(paths, "BTC-USD", "ONE_HOUR")
    assert to_epoch(seed["bar_open_time"])[0] == T0 and len(seed) == 2000


def test_a_seed_manifest_without_a_requested_start_sorts_last(paths):
    def freeze(n, start, **extra):
        bars, rep = validate_bars(make_raw(n, start=start), "coinbase", "BTC-USD", "ONE_HOUR", start,
                                  start + n * HOUR)
        freeze_dataset(paths, bars, rep, extra=extra)

    freeze(100, T0)
    freeze(50, T0 + 200 * HOUR, requested_start=None)
    seed = snapshot_seed(paths, "BTC-USD", "ONE_HOUR")
    assert to_epoch(seed["bar_open_time"])[0] == T0 and len(seed) == 100
