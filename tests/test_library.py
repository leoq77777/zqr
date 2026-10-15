"""Phase 1: decision cadence, seed templates (values and causality), seed catalog, library store and CLI."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest
import yaml

from quant_research.cli import main
from quant_research.data.bars import to_utc
from quant_research.library.catalog import Catalog, LibraryError, default_catalog_path, load_catalog
from quant_research.library.store import LibraryStore
from quant_research.strategy.cadence import boundaries, sample_and_hold
from quant_research.strategy.compute import compute_targets
from quant_research.strategy.spec import StrategySpec
from quant_research.strategy.templates import baz_response, get_template

HOUR = 3600
MON = 1704067200  # 2024-01-01T00:00:00Z, a Monday


def hourly(n: int, start: int = MON, seed: int = 0, vol: float = 0.01) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(0.0001 + vol * rng.standard_normal(n)))
    return pd.DataFrame({"bar_open_time": to_utc(start + HOUR * np.arange(n)), "open": close,
                         "high": close * 1.002, "low": close * 0.998, "close": close})


def last_boundary_reference(at: np.ndarray, value_at) -> np.ndarray:
    """Bar-by-bar reference: the value decided at the latest boundary, 0 before any."""
    out, held = np.zeros(len(at)), 0.0
    for i in range(len(at)):
        if at[i]:
            v = value_at(i)
            held = 0.0 if v is None or np.isnan(v) else float(v)
        out[i] = held
    return out


# -- cadence ---------------------------------------------------------------------------------------
def test_daily_boundary_is_the_bar_closing_at_midnight():
    b = boundaries(hourly(24 * 10), "daily")
    assert list(np.flatnonzero(b)) == list(range(23, 240, 24))


def test_weekly_boundary_is_monday_and_monthly_is_the_first():
    bars = hourly(24 * 70)
    assert list(np.flatnonzero(boundaries(bars, "weekly"))[:3]) == [167, 335, 503]
    times = bars["bar_open_time"][boundaries(bars, "monthly")] + pd.Timedelta(hours=1)
    assert [str(t.date()) for t in times] == ["2024-02-01", "2024-03-01"]
    assert all(t.hour == 0 for t in times)


def test_boundary_after_a_gap_falls_on_the_first_bar_past_midnight():
    bars = hourly(72).drop(index=[23]).reset_index(drop=True)  # the bar closing at 00:00 on day 2 is missing
    b = boundaries(bars, "daily")
    first = int(np.flatnonzero(b)[0])
    assert bars["bar_open_time"][first] == pd.Timestamp("2024-01-02 00:00", tz="UTC")


def test_bar_cadence_decides_every_bar_and_unknown_cadence_fails():
    assert boundaries(hourly(5), "bar").all()
    with pytest.raises(ValueError):
        boundaries(hourly(5), "yearly")


def test_sample_and_hold():
    sig = np.array([0.3, 0.4, np.nan, 0.6, 0.7, 0.8])
    at = np.array([False, True, True, False, True, False])
    assert list(sample_and_hold(sig, at)) == [0.0, 0.4, 0.0, 0.0, 0.7, 0.7]


# -- template values against bar-by-bar references --------------------------------------------------
@pytest.mark.parametrize("cadence", ["bar", "daily", "weekly", "monthly"])
def test_tsmom_matches_reference(cadence):
    bars = hourly(24 * 75, seed=3)
    lookback = 120
    close = bars["close"].to_numpy()
    ref = last_boundary_reference(boundaries(bars, cadence),
                                  lambda i: None if i < lookback else float(close[i] / close[i - lookback] > 1))
    out = get_template("tsmom")(bars, {"lookback": lookback, "cadence": cadence}).to_numpy()
    assert np.array_equal(out, ref)


@pytest.mark.parametrize("cadence", ["bar", "daily", "monthly"])
def test_ma_filter_matches_reference(cadence):
    bars = hourly(24 * 75, seed=4)
    window = 200
    close = bars["close"].to_numpy()
    ref = last_boundary_reference(
        boundaries(bars, cadence),
        lambda i: None if i < window - 1 else float(close[i] > close[i - window + 1:i + 1].mean()))
    out = get_template("ma_filter")(bars, {"window": window, "cadence": cadence}).to_numpy()
    assert np.array_equal(out, ref)


def test_baz_response_shape():
    assert baz_response(0.0) == 0.0
    peak = math.sqrt(2) * math.exp(-0.5) / 0.89
    assert baz_response(math.sqrt(2)) == pytest.approx(peak)
    assert peak == pytest.approx(0.9638, abs=1e-4)
    zs = np.linspace(0, 5, 501)
    assert baz_response(zs).max() == pytest.approx(peak, abs=1e-4)
    assert baz_response(-1.0) == -baz_response(1.0)


def _ewma(x: np.ndarray, n: int) -> np.ndarray:
    lam, out = (n - 1) / n, np.empty(len(x))
    out[0] = x[0]
    for t in range(1, len(x)):
        out[t] = lam * out[t - 1] + (1 - lam) * x[t]
    return out


def _rolling_std(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    for t in range(w - 1, len(x)):
        seg = x[t - w + 1:t + 1]
        if not np.isnan(seg).any():
            out[t] = seg.std(ddof=1)
    return out


def test_ewma_trend_matches_reference():
    bars = hourly(24 * 160, seed=5, vol=0.004)
    p = {"fast": 24 * 4, "slow": 24 * 12, "vol_window": 24 * 20, "norm_window": 24 * 40}
    at = boundaries(bars, "daily")
    daily = bars["close"].to_numpy()[at]
    x = _ewma(daily, 4) - _ewma(daily, 12)
    y = x / _rolling_std(daily, 20)
    z = y / _rolling_std(y, 40)
    u = np.maximum(baz_response(z), 0.0)
    k = {int(i): j for j, i in enumerate(np.flatnonzero(at))}
    ref = last_boundary_reference(at, lambda i: u[k[i]])
    out = get_template("ewma_trend")(bars, p).to_numpy()
    assert np.allclose(out, ref, atol=1e-12)
    assert (out > 0).any() and out.max() <= 0.9639


def test_vol_target_matches_reference_and_caps_at_one():
    bars = hourly(24 * 90, seed=6, vol=0.02)
    at = boundaries(bars, "daily")
    daily = bars["close"].to_numpy()[at]
    rets = np.concatenate([[np.nan], daily[1:] / daily[:-1] - 1])
    vol = _rolling_std(rets, 30) * math.sqrt(365)
    k = {int(i): j for j, i in enumerate(np.flatnonzero(at))}
    for target in (0.4, 5.0):
        ref = last_boundary_reference(
            at, lambda i, t=target: None if np.isnan(vol[k[i]]) else min(1.0, t / vol[k[i]]))
        out = get_template("vol_target")(bars, {"window": 720, "target_vol": target}).to_numpy()
        assert np.allclose(out, ref, atol=1e-12)
    assert (out[at][31:] == 1.0).all()  # a 500% target never binds


def test_daily_templates_need_whole_days():
    with pytest.raises(ValueError, match="whole number of days"):
        get_template("ewma_trend").validate({"fast": 100, "slow": 576, "vol_window": 2160, "norm_window": 8760})
    with pytest.raises(ValueError):
        get_template("vol_target").validate({"window": 730, "target_vol": 0.4})
    with pytest.raises(ValueError):
        get_template("tsmom").validate({"lookback": 168, "cadence": "hourly"})


# -- causality on the real seed parameters -----------------------------------------------------------
SEED_BARS = hourly(24 * 520, start=1609459200, seed=7)  # 2021-01-01, long enough for every seed to trade


@pytest.mark.parametrize("entry", [e.id for e in load_catalog().entries])
def test_seed_is_causal(entry):
    """Truncating later data, or changing it, never changes an earlier exposure."""
    e = next(x for x in load_catalog().entries if x.id == entry)
    spec = e.spec()
    bars = SEED_BARS
    n = len(bars)
    full = get_template(e.template)(bars, spec.parameters).to_numpy()
    assert (full > 0).any(), "seed never trades on the test data; the causality test would be vacuous"
    daily = np.flatnonzero(boundaries(bars, "daily"))
    positions = sorted({int(p) for p in np.linspace(n // 2, n - 2, 6).round()} | {int(daily[-3]), int(daily[-3]) + 1})
    tr = compute_targets(spec, bars, positions=positions)
    assert tr.lookahead_ok, f"look-ahead at bars {tr.lookahead_violations}"
    cut = int(daily[-10])
    changed = bars.copy()
    rng = np.random.default_rng(1)
    shock = np.exp(np.cumsum(0.05 * rng.standard_normal(n - cut - 1)))
    for col in ("open", "high", "low", "close"):
        changed.loc[cut + 1:, col] = changed[col].to_numpy()[cut + 1:] * shock
    out = get_template(e.template)(changed, spec.parameters).to_numpy()
    assert np.array_equal(out[:cut + 1], full[:cut + 1])


# -- seed catalog ----------------------------------------------------------------------------------
AGREED = {  # docs/library/seeds.md, approved 2026-10-13 (R1-R6)
    "tsmom-1w": ("tsmom", {"lookback": 168, "cadence": "weekly"}),
    "tsmom-1m": ("tsmom", {"lookback": 720, "cadence": "monthly"}),
    "tsmom-3m": ("tsmom", {"lookback": 2160, "cadence": "monthly"}),
    "tsmom-12m": ("tsmom", {"lookback": 8760, "cadence": "monthly"}),
    "ewma-8-24": ("ewma_trend", {"fast": 192, "slow": 576, "vol_window": 2160, "norm_window": 8760}),
    "ewma-16-48": ("ewma_trend", {"fast": 384, "slow": 1152, "vol_window": 2160, "norm_window": 8760}),
    "ewma-32-96": ("ewma_trend", {"fast": 768, "slow": 2304, "vol_window": 2160, "norm_window": 8760}),
    "turtle-s1": ("breakout", {"entry": 480, "exit": 240}),
    "turtle-s2": ("breakout", {"entry": 1320, "exit": 480}),
    "faber-10m": ("ma_filter", {"window": 7200, "cadence": "monthly"}),
    "brock-1-200": ("ma_filter", {"window": 4800, "cadence": "daily"}),
    "voltarget-1m": ("vol_target", {"window": 720, "target_vol": 0.40}),
}


def test_packaged_catalog_is_the_agreed_one():
    c = load_catalog()
    assert {e.id: (e.template, e.parameters) for e in c.entries} == AGREED
    assert c.products == ["BTC-USD", "ETH-USD"] and c.granularity == "ONE_HOUR"
    sleeves = [f.id for f in c.families if f.sleeve]
    assert sleeves == ["tsmom", "ewma_trend", "channel_breakout", "trend_filter"]
    for f in sleeves:
        assert sum(e.family == f for e in c.entries) >= 2
        assert c.member_share(f) <= 0.15
    assert [e.id for e in c.entries if e.kind == "overlay"] == ["voltarget-1m"]
    assert not any("reversal" in e.id for e in c.entries)
    insts = c.instances()
    assert len(insts) == 24 and len({i.id for i in insts}) == 24
    assert {i.id for i in insts if i.entry == "tsmom-12m"} == {"btc-tsmom-12m", "eth-tsmom-12m"}


def _raw() -> dict:
    return yaml.safe_load(default_catalog_path().read_text())


def _invalid(raw: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        Catalog.model_validate(raw)


def test_catalog_rejects_a_single_variant_sleeve():
    raw = _raw()
    raw["entries"] = [e for e in raw["entries"] if e["id"] != "turtle-s2"]
    _invalid(raw, "needs at least 2 variants")


def test_catalog_rejects_a_member_share_above_the_cap():
    raw = _raw()
    raw["max_member_share"] = 0.10
    _invalid(raw, "exceeds the cap")


def test_catalog_rejects_bad_entries():
    for change, match in (({"template": "nope"}, "unknown template"),
                          ({"kind": "overlay"}, "does not match family"),
                          ({"family": "ghost"}, "unknown family"),
                          ({"parameters": {"lookback": 168, "cadence": "weekly", "x": 1}}, "Extra inputs")):
        raw = _raw()
        raw["entries"][0].update(change)
        _invalid(raw, match)
    raw = _raw()
    raw["entries"].append(dict(raw["entries"][0]))
    _invalid(raw, "duplicate entry id")


def test_load_catalog_reports_the_file(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\n")
    with pytest.raises(LibraryError, match="bad.yaml"):
        load_catalog(bad)


# -- library store ---------------------------------------------------------------------------------
def test_import_is_idempotent_and_records_candidates(paths):
    store = LibraryStore(paths.db)
    c = load_catalog()
    plan = store.plan_import(c)
    assert len(plan["added"]) == 24 and not store.entries()  # planning writes nothing
    store.import_catalog(c)
    rows = store.entries()
    assert len(rows) == 24 and {r["status"] for r in rows} == {"candidate"}
    assert len(store.families()) == 5
    row = store.entry("eth-faber-10m")
    assert row["parameters"] == {"window": 7200, "cadence": "monthly"} and row["product"] == "ETH-USD"
    spec = StrategySpec(strategy_ref=row["strategy_ref"], parameters=row["parameters"])
    assert row["candidate_hash"] == spec.candidate_hash()
    assert [e["to_status"] for e in store.events("eth-faber-10m")] == ["candidate"]
    again = store.import_catalog(c)
    assert not again["added"] and len(again["unchanged"]) == 24
    assert len(store.events("eth-faber-10m")) == 1
    store.close()


def test_changing_an_imported_entry_is_refused_atomically(paths, tmp_path):
    store = LibraryStore(paths.db)
    store.import_catalog(load_catalog())
    raw = _raw()
    raw["entries"][0]["parameters"]["lookback"] = 336
    raw["entries"].append({**raw["entries"][1], "id": "tsmom-6m", "parameters": {"lookback": 4320,
                                                                                 "cadence": "monthly"}})
    changed = Catalog.model_validate(raw)
    with pytest.raises(LibraryError, match="btc-tsmom-1w: the catalog changes an imported entry"):
        store.import_catalog(changed)
    assert store.entry("btc-tsmom-6m") is None
    assert store.entry("btc-tsmom-1w")["parameters"]["lookback"] == 168
    store.close()


def test_status_transitions(paths):
    store = LibraryStore(paths.db)
    store.import_catalog(load_catalog())
    store.set_status("btc-turtle-s1", "member", "controller", "seed portfolio v1 in paper")
    with pytest.raises(LibraryError, match="member -> probation"):
        store.set_status("btc-turtle-s1", "probation", "controller", "x")
    store.set_status("btc-turtle-s1", "retired", "human", "reverse test")
    with pytest.raises(LibraryError, match="not allowed"):
        store.set_status("btc-turtle-s1", "member", "human", "x")
    with pytest.raises(LibraryError, match="unknown"):
        store.set_status("nope", "member", "human", "x")
    assert [e["to_status"] for e in store.events("btc-turtle-s1")] == ["candidate", "member", "retired"]
    store.close()


# -- CLI -------------------------------------------------------------------------------------------
def test_cli_import_list_show(paths, capsys):
    home = str(paths.root)
    main(["--home", home, "library", "list"])
    assert "library is empty" in capsys.readouterr().out
    main(["--home", home, "library", "import", "--dry-run"])
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] and len(out["added"]) == 24
    main(["--home", home, "library", "list"])
    assert "library is empty" in capsys.readouterr().out
    main(["--home", home, "library", "import"])
    assert len(json.loads(capsys.readouterr().out)["added"]) == 24
    main(["--home", home, "library", "list", "--product", "BTC-USD", "--family", "tsmom"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 5
    assert any("btc-tsmom-12m" in line and "lookback=8760" in line for line in lines)
    main(["--home", home, "library", "show", "btc-voltarget-1m"])
    shown = json.loads(capsys.readouterr().out)
    assert shown["kind"] == "overlay" and shown["parameters"]["target_vol"] == 0.4
    assert shown["meta"]["sources"][0].startswith("Moreira & Muir")
    assert shown["seed_run"] is None
    with pytest.raises(SystemExit, match="unknown library entry"):
        main(["--home", home, "library", "show", "nope"])
