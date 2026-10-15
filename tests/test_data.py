import json

import httpx
import numpy as np
import pytest
import respx

from quant_research.data.bars import to_epoch
from quant_research.data.coinbase import (
    BASE_URL,
    CoinbaseError,
    CoinbasePublicClient,
    RateLimiter,
    fetch_range,
    plan_windows,
    raw_dir_for,
)
from quant_research.data.snapshot import SnapshotError, freeze_dataset, load_dataset
from quant_research.data.validate import load_raw_candles, validate_bars
from tests.conftest import HOUR, T0, make_raw


def test_plan_windows_aligned_and_capped():
    w = plan_windows(T0 + 10, T0 + 1000 * HOUR, "ONE_HOUR")
    assert w[0][0] == T0 + HOUR  # ceil to grid
    assert all((we - ws) // HOUR <= 350 for ws, we in w)
    assert w[-1][1] == T0 + 1000 * HOUR
    assert all(a[1] == b[0] for a, b in zip(w, w[1:], strict=False))


def _candles(ws, we_inclusive, step=HOUR):
    return {"candles": [{"start": str(t), "low": "1", "high": "3", "open": "2", "close": "2.5", "volume": "1"}
                        for t in range(we_inclusive, ws - 1, -step)]}  # API returns newest first


def _client(**kw):
    return CoinbasePublicClient(limiter=RateLimiter(1e6), sleep=lambda s: None, backoff_base=0, **kw)


@respx.mock
def test_fetch_range_resumes_and_retries(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429)
        p = request.url.params
        return httpx.Response(200, json=_candles(int(p["start"]), int(p["end"])))

    respx.get(url__startswith=f"{BASE_URL}/market/products/BTC-USD/candles").mock(side_effect=handler)
    end = T0 + 400 * HOUR
    s1 = fetch_range(_client(), "BTC-USD", "1h", T0, end, tmp_path, now=lambda: end + 10 * HOUR)
    assert (s1.windows, s1.fetched, s1.candles) == (2, 2, 400)
    assert len(calls) == 3  # one 429 retried
    s2 = fetch_range(_client(), "BTC-USD", "1h", T0, end, tmp_path, now=lambda: end + 10 * HOUR)
    assert (s2.fetched, s2.resumed) == (0, 2)
    assert len(calls) == 3
    log = (raw_dir_for(tmp_path, "BTC-USD", "ONE_HOUR") / "requests.jsonl").read_text().splitlines()
    assert len(log) == 2 and json.loads(log[0])["attempts"] == 2


@respx.mock
def test_fetch_refetches_window_that_was_open(tmp_path):
    respx.get(url__startswith=f"{BASE_URL}/market/products/BTC-USD/candles").mock(
        side_effect=lambda r: httpx.Response(200, json=_candles(int(r.url.params["start"]),
                                                                int(r.url.params["end"]))))
    end = T0 + 10 * HOUR
    fetch_range(_client(), "BTC-USD", "1h", T0, end, tmp_path, now=lambda: T0 + 5 * HOUR)
    s = fetch_range(_client(), "BTC-USD", "1h", T0, end, tmp_path, now=lambda: end + HOUR)
    assert s.fetched == 1 and s.resumed == 0


@respx.mock
def test_client_raises_on_client_error():
    respx.get(url__startswith=BASE_URL).mock(return_value=httpx.Response(400, text="bad"))
    with pytest.raises(CoinbaseError):
        _client().candles("BTC-USD", T0, T0 + HOUR, "1h")


@respx.mock
def test_raw_roundtrip_clips_end_boundary(tmp_path):
    # Server includes the end boundary (observed behaviour); validation clips to [start, end).
    respx.get(url__startswith=BASE_URL).mock(
        side_effect=lambda r: httpx.Response(200, json=_candles(int(r.url.params["start"]),
                                                                int(r.url.params["end"]) + HOUR)))
    end = T0 + 24 * HOUR
    fetch_range(_client(), "BTC-USD", "1h", T0, end, tmp_path, now=lambda: end + HOUR)
    raw = load_raw_candles(raw_dir_for(tmp_path, "BTC-USD", "ONE_HOUR"))
    assert len(raw) == 25
    bars, rep = validate_bars(raw, "coinbase", "BTC-USD", "1h", T0, end)
    assert rep.rows == 24 and rep.out_of_range == 1
    assert to_epoch(bars["bar_open_time"])[-1] == end - HOUR


def test_validate_dedupes_drops_incomplete_and_invalid_and_reports_gaps():
    raw = make_raw(100)
    raw = raw.drop(index=[40, 41, 42])  # internal gap of 3 bars
    dup = raw.iloc[[10]].copy()
    dup["close"] += 1.0
    dup["high"] += 1.0
    dup["fetched_at"] += 1
    raw = __import__("pandas").concat([raw, dup])
    raw.loc[raw.index == 20, "high"] = 0.5 * raw.loc[raw.index == 20, "low"]  # OHLC inconsistent
    last = raw["start"].max()
    raw.loc[raw["start"] == last, "fetched_at"] = last + 100  # still forming
    end = T0 + 100 * HOUR
    bars, rep = validate_bars(raw, "coinbase", "BTC-USD", "1h", T0, end)
    assert rep.duplicates == 1 and rep.duplicate_conflicts == 1
    assert rep.incomplete_dropped == 1
    assert rep.invalid_dropped == {"ohlc_inconsistent": 1}
    assert [(g.start, g.bars) for g in rep.gaps] == [(T0 + 20 * HOUR, 1), (T0 + 40 * HOUR, 3)]
    assert rep.has_unfilled_gaps
    # Latest fetch wins on conflicting duplicates.
    assert bars.loc[to_epoch(bars["bar_open_time"]) == T0 + 10 * HOUR, "close"].item() == dup["close"].item()
    assert np.all(np.diff(to_epoch(bars["bar_open_time"])) > 0)


def test_fill_policy_is_explicit():
    raw = make_raw(50).drop(index=[10, 11])
    bars, rep = validate_bars(raw, "coinbase", "BTC-USD", "1h", T0, T0 + 50 * HOUR, fill_policy="ffill_flat")
    assert rep.filled_rows == 2 and not rep.has_unfilled_gaps
    filled = bars[bars["filled"]]
    assert (filled["volume"] == 0).all()
    assert (filled["open"] == bars.loc[9, "close"]).all()
    assert len(bars) == 50


def test_leading_missing_is_not_an_internal_gap():
    raw = make_raw(50, start=T0 + 10 * HOUR)
    _, rep = validate_bars(raw, "coinbase", "BTC-USD", "1h", T0, T0 + 60 * HOUR)
    assert rep.leading_missing_bars == 10 and rep.internal_gap_bars == 0


def test_isolated_prints_are_dropped_only_when_asked():
    raw = make_raw(60)
    prev = raw.loc[9, "close"]
    raw.loc[10, ["open", "high"]] = 1.5 * prev  # isolated open: the close stays near the previous close
    raw.loc[20, "low"] = 0.1  # isolated wick
    fall = raw.loc[30, "open"]
    raw.loc[30, ["high", "close", "low"]] = [fall, 0.75 * fall, 0.74 * fall]  # a genuine 25% fall

    bars, rep = validate_bars(raw, "coinbase", "BTC-USD", "1h", T0, T0 + 60 * HOUR, fill_policy="ffill_flat")
    assert rep.invalid_dropped == {} and not bars["filled"].any()

    bars, rep = validate_bars(raw, "coinbase", "BTC-USD", "1h", T0, T0 + 60 * HOUR, fill_policy="ffill_flat",
                              drop_isolated_prints=True)
    assert rep.invalid_dropped == {"isolated_print": 2}
    assert [p["start"] for p in rep.isolated_prints] == [T0 + 10 * HOUR, T0 + 20 * HOUR]
    assert rep.isolated_prints[1]["low"] == 0.1
    assert len(bars) == 60 and bars.loc[[10, 20], "filled"].all() and not bars.loc[30, "filled"]
    assert bars.loc[10, "open"] == prev and bars.loc[30, "close"] == 0.75 * fall


def test_snapshot_is_immutable_and_verified(paths):
    raw = make_raw(100)
    bars, rep = validate_bars(raw, "coinbase", "BTC-USD", "1h", T0, T0 + 100 * HOUR)
    m1 = freeze_dataset(paths, bars, rep)
    m2 = freeze_dataset(paths, bars, rep)
    assert m1["dataset_id"] == m2["dataset_id"]
    loaded, manifest = load_dataset(paths, m1["dataset_id"])
    assert len(loaded) == 100 and manifest["coverage"] == 1.0
    pq = paths.snapshots / m1["dataset_id"] / "bars.parquet"
    pq.chmod(0o644)
    tampered = loaded.copy()
    tampered.loc[5, "close"] *= 2
    tampered.to_parquet(pq, index=False)
    with pytest.raises(SnapshotError):
        load_dataset(paths, m1["dataset_id"])


def _frozen(paths, n, start, seed, drop=(), fill="ffill_flat"):
    raw = make_raw(n, start=start, seed=seed).drop(index=list(drop))
    bars, rep = validate_bars(raw, "coinbase", "BTC-USD", "1h", start, start + n * HOUR, fill_policy=fill)
    return bars, rep


def test_extend_dataset_keeps_the_base_rows_verbatim(paths):
    from quant_research.data.snapshot import content_hash, extend_dataset

    base_bars, base_rep = _frozen(paths, 100, T0 + 60 * HOUR, seed=1, drop=(30, 31))
    base = freeze_dataset(paths, base_bars, base_rep)
    prefix, prep = _frozen(paths, 60, T0, seed=2, drop=(10,))
    m = extend_dataset(paths, base["dataset_id"], prefix, prep)
    assert m["extends"] == base["dataset_id"] and m["base_data_hash"] == base["data_hash"]
    assert m["rows"] == 160 and m["actual_first_open"] == T0 and m["actual_last_open"] == base["actual_last_open"]
    assert m["requested_start"] == T0 and m["requested_end"] == base["requested_end"]
    assert m["internal_gap_bars"] == 3 and m["filled_rows"] == 3
    assert [g["start"] for g in m["gaps"]] == [T0 + 10 * HOUR, T0 + 90 * HOUR]
    assert m["coverage"] == round(157 / 160, 6)
    assert m["junction"]["base_first_open"] == T0 + 60 * HOUR
    bars, _ = load_dataset(paths, m["dataset_id"])
    assert content_hash(bars.iloc[60:].reset_index(drop=True)) == base["data_hash"]
    assert extend_dataset(paths, base["dataset_id"], prefix, prep)["dataset_id"] == m["dataset_id"]


def test_extend_dataset_refuses_gaps_overlaps_and_mismatches(paths):
    from quant_research.data.snapshot import extend_dataset

    base = freeze_dataset(paths, *_frozen(paths, 100, T0 + 60 * HOUR, seed=1))
    with pytest.raises(SnapshotError, match="gap before"):
        extend_dataset(paths, base["dataset_id"], *_frozen(paths, 59, T0, seed=2))
    with pytest.raises(SnapshotError, match="overlaps"):
        extend_dataset(paths, base["dataset_id"], *_frozen(paths, 61, T0, seed=2))
    with pytest.raises(SnapshotError, match="fill_policy"):
        extend_dataset(paths, base["dataset_id"], *_frozen(paths, 60, T0, seed=2, fill="none"))
    prefix, prep = _frozen(paths, 60, T0, seed=2)
    with pytest.raises(SnapshotError, match="instrument"):
        extend_dataset(paths, base["dataset_id"], prefix, prep.model_copy(update={"instrument": "ETH-USD"}))


def test_extend_dataset_refuses_a_synthetic_base(paths):
    from quant_research.data.snapshot import extend_dataset
    from quant_research.data.synthetic import SyntheticSpec, freeze_synthetic

    m = freeze_synthetic(paths, SyntheticSpec(kind="random_walk", instrument="X-USD", seed=1, bars=100))
    with pytest.raises(SnapshotError, match="synthetic"):
        extend_dataset(paths, m["dataset_id"], *_frozen(paths, 60, T0, seed=2))


def test_extend_dataset_command_reads_the_raw_windows(paths, capsys):
    from quant_research.cli import main

    base = freeze_dataset(paths, *_frozen(paths, 100, T0 + 60 * HOUR, seed=1))
    raw_dir = raw_dir_for(paths.raw, "BTC-USD", "ONE_HOUR")
    raw_dir.mkdir(parents=True)
    raw = make_raw(60, seed=2)
    candles = [{"start": str(int(r.start)), **{c: str(getattr(r, c)) for c in ("open", "high", "low", "close",
                                                                             "volume")}} for r in raw.itertuples()]
    (raw_dir / f"{T0}_{T0 + 60 * HOUR}.json").write_text(json.dumps({
        "status": 200, "venue": "coinbase", "product_id": "BTC-USD", "granularity": "ONE_HOUR",
        "source_version": "test", "fetched_at": T0 + 200 * HOUR, "body": {"candles": candles}}))
    main(["--home", str(paths.root), "extend-dataset", "--dataset", base["dataset_id"], "--start",
          str(T0), "--fill-policy", "ffill_flat"])
    out = json.loads(capsys.readouterr().out)
    assert out["extends"] == base["dataset_id"] and out["rows"] == 160 and out["prefix_rows"] == 60
    main(["--home", str(paths.root), "datasets"])
    listed = json.loads(capsys.readouterr().out)
    assert [d.get("extends") for d in listed if d["dataset_id"] == out["dataset_id"]] == [base["dataset_id"]]
