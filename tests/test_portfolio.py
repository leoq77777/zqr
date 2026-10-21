import math

import numpy as np
import pandas as pd
import pytest

from quant_research.backtest.engine import EngineConfig, run_backtest
from quant_research.data.validate import validate_bars
from quant_research.portfolio.backtest import run_portfolio
from quant_research.portfolio.blend import Blend, blend, member_shares
from quant_research.portfolio.decide import Decider, DeciderState
from quant_research.portfolio.version import (
    Ladder,
    Member,
    Overlay,
    PortfolioVersion,
    VersionError,
    VersionStore,
)
from quant_research.strategy.compute import check_positions
from tests.conftest import HOUR, T0, make_raw


def _bars(n=1500, seed=3, drift=0.0002, vol=0.01):
    bars, _ = validate_bars(make_raw(n, seed=seed, drift=drift, vol=vol), "coinbase", "BTC-USD", "ONE_HOUR", T0,
                            T0 + n * HOUR)
    return bars


def _member(eid, family, ref="template:buy_and_hold", **params):
    return Member(entry_id=eid, family=family, strategy_ref=ref, parameters=params)


def _version(members, overlay=None, **kw):
    return PortfolioVersion(product="BTC-USD", dataset_id="ds", split_id="split-x", members=tuple(members),
                            overlay=overlay, engine_config=EngineConfig(), **kw)


# -- versions -------------------------------------------------------------------------------------

def test_version_id_is_a_hash_of_the_body():
    v = _version([_member("a", "f")])
    assert v.version_id().startswith("pv-") and v.version_id() == _version([_member("a", "f")]).version_id()
    assert _version([_member("a", "f")], band=0.05).version_id() != v.version_id()
    assert _version([_member("a", "f", "template:sma_cross", fast=5, slow=20)]).version_id() != v.version_id()
    assert _version([_member("a", "f")], ladder=Ladder(recover=-0.10)).version_id() != v.version_id()


def test_version_refuses_settings_paper_cannot_reproduce():
    with pytest.raises(ValueError, match="deadband"):
        PortfolioVersion(product="BTC-USD", dataset_id="ds", split_id="s", members=(_member("a", "f"),),
                         engine_config=EngineConfig(rebalance_tolerance=0.05))
    with pytest.raises(ValueError, match="duplicate"):
        _version([_member("a", "f"), _member("a", "g")])


def test_version_store_records_every_transition(store):
    vs = VersionStore(store)
    vid = vs.put(_version([_member("a", "f")]))
    assert vs.put(_version([_member("a", "f")])) == vid and vs.get(vid)["state"] == "draft"
    with pytest.raises(VersionError):
        vs.transition(vid, "frozen", "human")
    vs.transition(vid, "validated", "controller", {"sharpe": 1})
    with pytest.raises(VersionError):
        vs.transition(vid, "frozen", "controller")
    vs.transition(vid, "frozen", "human", note="ok")
    vs.transition(vid, "final_evaluated", "evaluator")
    with pytest.raises(VersionError):
        vs.transition(vid, "paper", "controller")
    row = vs.transition(vid, "paper", "human")
    assert row["state"] == "paper" and row["evidence"] == {"sharpe": 1}
    assert [e["to_state"] for e in vs.events(vid)] == ["draft", "validated", "frozen", "final_evaluated", "paper"]
    assert vs.version(vid) == _version([_member("a", "f")])


# -- blend ----------------------------------------------------------------------------------------

def test_member_shares_use_equal_sleeves_and_the_cap():
    fams = [("tsmom", 4), ("ewma", 3), ("breakout", 2), ("filter", 2)]
    v = _version([_member(f"{f}{k}", f) for f, n in fams for k in range(n)])
    s = member_shares(v)
    assert s["tsmom0"] == pytest.approx(0.0625) and s["ewma0"] == pytest.approx(1 / 12)
    assert s["breakout0"] == s["filter1"] == pytest.approx(0.125)
    capped = member_shares(_version([_member("a", "f"), _member("b", "g")]))
    assert capped == {"a": 0.15, "b": 0.15}
    prob = member_shares(_version([_member("a", "f"), Member(entry_id="b", family="f", strategy_ref="template:cash",
                                                              status="probation")], member_cap=1.0))
    assert prob == {"a": 0.5, "b": 0.25}


def test_blend_matches_hand_calculation_and_the_overlay_scales_it():
    bars = _bars(300)
    members = [_member("a", "f"), _member("b", "f", "template:cash"), _member("c", "g")]
    bl = blend(_version(members, member_cap=1.0), bars)
    assert np.allclose(bl.target, 0.75) and np.allclose(bl.families["f"], 0.25)
    flat = blend(_version(members, Overlay(entry_id="o", strategy_ref="template:cash"), member_cap=1.0), bars)
    assert np.allclose(flat.target, 0.0) and np.allclose(flat.pre_overlay, 0.75)


def test_plugin_members_run_only_in_the_docker_sandbox(monkeypatch):
    from quant_research.portfolio import blend as blend_mod
    from quant_research.portfolio.blend import BlendError, has_plugin
    from quant_research.strategy.compute import TargetResult
    from quant_research.strategy.sandbox import SandboxConfig

    bars = _bars(200)
    v = _version([_member("a", "f"), _member("p", "g", "plugin:" + "a" * 64)], member_cap=1.0)
    assert has_plugin(v) and not has_plugin(_version([_member("a", "f")]))
    with pytest.raises(BlendError, match="docker sandbox"):
        blend(v, bars)
    with pytest.raises(BlendError, match="docker sandbox"):
        blend(v, bars, sandbox=SandboxConfig())
    seen = []

    def fake(spec, bars_, paths, sandbox=None, positions=None):
        seen.append((spec.kind, sandbox.isolation if sandbox else None))
        return TargetResult(np.full(len(bars_), 0.5 if spec.kind == "plugin" else 1.0))
    monkeypatch.setattr(blend_mod, "compute_targets", fake)
    bl = blend(v, bars, sandbox=SandboxConfig(isolation="docker"))
    assert np.allclose(bl.target, 0.75) and ("plugin", "docker") in seen


def test_paper_refuses_plugin_versions(paths, store):
    from quant_research.portfolio.paper import start_shadow

    vs = VersionStore(store)
    vid = vs.put(_version([_member("a", "f"), _member("p", "g", "plugin:" + "a" * 64)]))
    vs.transition(vid, "failed_validation", "controller")
    with pytest.raises(VersionError, match="paper runs template strategies only"):
        start_shadow(paths, store, vid, "forward evidence")


def test_blend_has_no_lookahead():
    bars = _bars(1500)
    v = _version([_member("a", "f", "template:sma_cross", fast=5, slow=20), _member("b", "g")],
                 Overlay(entry_id="o", strategy_ref="template:vol_target", parameters={"window": 720,
                                                                                      "target_vol": 0.4}))
    full = blend(v, bars).target
    assert full.min() >= 0 and full.max() <= 1 and len(set(np.round(full, 6))) > 3
    for i in check_positions(np.ones(len(bars), bool)):
        assert blend(v, bars.iloc[: i + 1]).target[-1] == pytest.approx(full[i])


# -- decision step --------------------------------------------------------------------------------

def _decider(**kw):
    return Decider(0.10, Ladder(), 1.0, **kw)


def test_price_drift_alone_never_trades():
    d = _decider(state=DeciderState(last_target=0.5, peak=100))
    assert math.isnan(d.decide(0.5, 100, 50)) and math.isnan(d.decide(0.5, 105, 59))


def test_band_and_full_exit():
    d = _decider()
    assert math.isnan(d.decide(0.05, 100, 0))  # the account starts in cash
    assert d.decide(0.5, 100, 0) == 0.5
    d.executed(0.5)
    assert math.isnan(d.decide(0.58, 100, 50)) and d.decide(0.61, 100, 50) == 0.61
    d.executed(0.05)
    assert d.decide(0.0, 100, 5) == 0.0
    d.executed(0.0)
    assert math.isnan(d.decide(0.0, 100, 0.5))  # dust below the minimum notional


def test_ladder_halves_recovers_and_liquidates():
    d = _decider()
    d.executed(d.decide(0.8, 100, 0))
    assert d.decide(0.8, 74.9, 60) == pytest.approx(0.4) and d.state.level == "halved" and d.state.yellow
    d.executed(0.4)
    assert math.isnan(d.decide(0.8, 80, 30))  # still halved, inside the band
    assert d.decide(0.8, 85.1, 30) == pytest.approx(0.8) and d.state.level == "full"
    d.executed(0.8)
    assert d.decide(0.8, 69.9, 55) == 0.0 and d.state.level == "liquidated"
    assert d.decide(0.9, 120, 40) == 0.0 and d.state.peak == 100  # halted: exits only, the peak is frozen
    d.executed(0.0)
    assert math.isnan(d.decide(0.9, 120, 0))
    d.resume(120)
    assert d.state.peak == 120 and d.decide(0.9, 120, 0) == pytest.approx(0.9) and d.state.level == "full"


def test_yellow_flag_is_reported_without_action():
    d = _decider(state=DeciderState(last_target=0.5, peak=100))
    assert math.isnan(d.decide(0.5, 79, 40)) and d.state.yellow and d.state.level == "full"


def test_state_round_trips_through_a_dict():
    import json

    d = _decider()
    d.decide(0.5, np.float64(100.0), 0.0)
    s = d.state
    assert DeciderState.from_dict(json.loads(json.dumps(s.to_dict()))) == s
    assert DeciderState.from_dict(None) == DeciderState()


# -- engine hook and portfolio backtest -----------------------------------------------------------

class Recorder:
    def __init__(self, targets):
        self.targets, self.seen, self.done = targets, [], []

    def decide(self, i, equity, position):
        self.seen.append((i, equity, position))
        return self.targets[i]

    def executed(self, i, target):
        self.done.append(i)


def test_engine_asks_the_decider_with_the_state_at_the_decision_close():
    bars = _bars(200)
    targets = np.where(np.arange(200) % 50 < 25, 1.0, 0.0)
    rec = Recorder(targets)
    res = run_backtest(bars, targets, EngineConfig(), decider=rec)
    eq = res.equity
    for i, equity, position in rec.seen[1:]:
        assert equity == pytest.approx(eq["equity"][i]) and position == pytest.approx(eq["qty"][i] * eq["close"][i])
    assert sorted(rec.done) == sorted(int(b) - 1 for b in res.fills["bar_index"])
    plain = run_backtest(bars, targets, EngineConfig())
    assert plain.equity.equals(res.equity)


def test_decider_requires_zero_delay():
    bars = _bars(50)
    with pytest.raises(ValueError, match="delay"):
        run_backtest(bars, np.ones(50), EngineConfig(execution_delay_bars=1), decider=Recorder(np.ones(50)))


def _fixed_blend(target):
    n = len(target)
    return Blend({}, {}, np.asarray(target, float), np.ones(n), np.asarray(target, float))


def test_run_portfolio_respects_the_deadband():
    bars = _bars(200)
    v = _version([_member("a", "f")])
    calm = run_portfolio(v, bars, bl=_fixed_blend(np.where(np.arange(200) % 2, 0.55, 0.50)))
    assert len(calm.result.fills) == 1
    busy = run_portfolio(v, bars, bl=_fixed_blend(np.where(np.arange(200) % 2, 0.6, 0.4)))
    assert len(busy.result.fills) == 199  # every decision bar
    log = calm.log
    assert len(log) == 199 and log["executed"].sum() == 1 and log["pre_state"][0]["last_target"] == 0.0


def test_ladder_liquidates_a_crash_in_the_backtest():
    bars = _bars(600, drift=-0.002, vol=0.002)
    run = run_portfolio(_version([_member("a", "f")]), bars, bl=_fixed_blend(np.ones(600)))
    assert run.state.level == "liquidated" and run.result.equity["qty"].iloc[-1] == 0.0
    assert set(run.log["level"]) == {"full", "halved", "liquidated"}
    first_halved = run.log.index[run.log["level"] == "halved"][0]
    assert run.log["order_target"][first_halved] == pytest.approx(0.5)
    assert pd.isna(run.log["order_target"].iloc[-1])


# -- dev and validation runs, seed portfolio (T5, T6) ----------------------------------------------------------

def _desk(paths, store, dataset):
    from quant_research.desk.config import DeskConfig, save_config
    from quant_research.library.catalog import load_catalog
    from quant_research.library.store import LibraryStore
    from tests.conftest import make_mandate

    m = make_mandate(dataset)
    store.put_mandate(m.mandate_id(), m.model_dump(mode="json"))
    save_config(paths, DeskConfig(mandates={"BTC-USD": m.mandate_id()}))
    lib = LibraryStore(paths.db)
    lib.import_catalog(load_catalog())
    lib.close()


def _seeded(paths, store, dataset):
    from quant_research.portfolio.seed import define_seed

    _desk(paths, store, dataset)
    return define_seed(paths, store, "BTC-USD")


def test_seed_portfolio_takes_every_member_and_the_overlay(paths, store, dataset):
    from quant_research.portfolio.seed import seed_version

    _desk(paths, store, dataset)
    v = seed_version(paths, store, "BTC-USD")
    manifest, split = dataset
    assert v.dataset_id == manifest["dataset_id"] and v.split_id == split.split_id
    assert v.overlay is not None and v.overlay.strategy_ref == "template:vol_target"
    assert {m.family for m in v.members} == {"tsmom", "ewma_trend", "channel_breakout", "trend_filter"}
    assert len(v.members) == 11 and v.band == 0.10 and v.ladder == Ladder()
    assert member_shares(v)["btc-tsmom-1w"] == pytest.approx(0.0625)
    assert all(m.entry_id.startswith("btc-") for m in v.members)
    assert v.engine_config.rebalance_tolerance == 0.0
    assert seed_version(paths, store, "BTC-USD").version_id() == v.version_id()
    with pytest.raises(VersionError, match="no desk mandate"):
        seed_version(paths, store, "ETH-USD")


def test_dev_run_is_an_archived_trial_and_validation_is_counted(paths, store, dataset):
    from quant_research.memory.returns import ReturnArchive
    from quant_research.portfolio.evaluate import run_segment

    vid = _seeded(paths, store, dataset)
    split_id = dataset[1].split_id
    trials = store.trial_count(split_id)
    dev = run_segment(paths, store, vid, "dev")
    assert store.trial_count(split_id) == trials + 1
    assert dev["kind"] == "portfolio" and dev["strategy_ref"] == f"portfolio:{vid}" and dev["segment"] == "dev"
    assert dev["candidate_hash"] == vid and dev["status"] == "completed"
    for key in ("sharpe_daily", "max_drawdown_no_ladder", "ladder_time", "family_contribution", "mean_target"):
        assert key in dev["metrics"]
    assert "buy_and_hold" in dev["benchmarks"]
    archived = ReturnArchive(paths, store).index(dataset[1].split_id)
    assert [r["experiment_id"] for r in archived] == [dev["id"]] and archived[0]["n_days"] > 0
    assert {"sharpe_daily_no_ladder", "total_return_no_ladder"} <= set(dev["metrics"])
    assert (paths.results / dev["id"] / "equity_no_ladder.parquet").exists()
    val = run_segment(paths, store, vid, "validation")
    assert val["kind"] == "portfolio_validation" and val["segment"] == "validation"
    assert store.trial_count(split_id) == trials + 1
    vs = VersionStore(store)
    assert vs.validation_uses(dataset[1].split_id) == 1
    assert (paths.results / val["id"] / "decisions.parquet").exists()
    again = run_segment(paths, store, vid, "validation")
    assert again["id"] == val["id"] and vs.validation_uses(dataset[1].split_id) == 1
    assert run_segment(paths, store, vid, "dev")["id"] == dev["id"]


def test_the_archive_reads_a_portfolio_run_without_the_ladder(paths, store):
    from quant_research.memory.returns import ReturnArchive, daily_returns, research_equity_file

    times = pd.date_range("2024-01-01", periods=24 * 40, freq="h", tz="UTC")
    falling = pd.DataFrame({"time": times, "equity": 100 * np.exp(np.linspace(0, -0.5, len(times)))})
    rising = pd.DataFrame({"time": times, "equity": 100 * np.exp(np.linspace(0, 0.3, len(times)))})
    eid = store.start_experiment(kind="portfolio", strategy_ref="portfolio:pv-x", parameters={}, candidate_hash="pv-x",
                                 dataset_id="d", data_hash="h", split_id="split-x", segment="dev",
                                 window_start=0, window_end=1, engine_config_id="e",
                                 engine_config={"initial_cash": 100.0}, engine_version="t")
    res = paths.results / eid
    res.mkdir(parents=True)
    falling.to_parquet(res / "equity.parquet", index=False)
    store.finish_experiment(eid, "completed", metrics={}, result_dir=str(res))
    row = store.get_experiment(eid)
    assert research_equity_file(paths, row).name == "equity.parquet"  # runs recorded before A13.6
    rising.to_parquet(res / "equity_no_ladder.parquet", index=False)
    assert research_equity_file(paths, row).name == "equity_no_ladder.parquet"
    archive = ReturnArchive(paths, store)
    archive.add([row])
    stored = archive.load("split-x")[eid].dropna()
    assert stored.to_numpy() == pytest.approx(daily_returns(rising, 100.0).to_numpy())


def test_validate_applies_the_seed_standard(paths, store, dataset, monkeypatch):
    from quant_research.portfolio import evaluate

    vid = _seeded(paths, store, dataset)
    monkeypatch.setitem(evaluate.SEED_STANDARD, "min_sharpe_daily", -100.0)
    monkeypatch.setitem(evaluate.SEED_STANDARD, "min_max_drawdown", -1.0)
    out = evaluate.validate(paths, store, vid)
    assert out["state"] == "validated" and out["passed"]
    assert out["validation"]["experiment_id"] != out["dev"]["experiment_id"]
    assert "max_drawdown_no_ladder" in out["validation"] and "sharpe_daily" in out["dev"]
    with pytest.raises(VersionError, match="only a draft"):
        evaluate.validate(paths, store, vid)
    assert VersionStore(store).get(vid)["evidence"]["validation"]["sharpe_daily"] == out["validation"]["sharpe_daily"]


def test_validate_fails_below_the_standard(paths, store, dataset, monkeypatch):
    from quant_research.portfolio import evaluate

    vid = _seeded(paths, store, dataset)
    monkeypatch.setitem(evaluate.SEED_STANDARD, "min_sharpe_daily", 100.0)
    out = evaluate.validate(paths, store, vid)
    assert out["state"] == "failed_validation" and not out["passed"]


def _lenient(monkeypatch):
    from quant_research.portfolio import evaluate

    monkeypatch.setitem(evaluate.SEED_STANDARD, "min_sharpe_daily", -100.0)
    monkeypatch.setitem(evaluate.SEED_STANDARD, "min_max_drawdown", -1.0)


def test_portfolio_commands_end_to_end(paths, store, dataset, capsys, monkeypatch):
    import json

    from quant_research.cli import main

    _desk(paths, store, dataset)
    _lenient(monkeypatch)
    home = str(paths.root)
    main(["--home", home, "portfolio", "seed", "--product", "BTC-USD"])
    seeded = json.loads(capsys.readouterr().out)
    vid = seeded["version_id"]
    assert seeded["state"] == "draft" and seeded["shares"]["btc-turtle-s1"] == pytest.approx(0.125)
    with pytest.raises(SystemExit, match="draft"):
        main(["--home", home, "portfolio", "freeze", vid, "--note", "too early"])
    with pytest.raises(SystemExit):
        main(["--home", home, "portfolio", "freeze", vid])
    capsys.readouterr()
    main(["--home", home, "portfolio", "validate", vid])
    assert json.loads(capsys.readouterr().out)["state"] == "validated"
    main(["--home", home, "portfolio", "list", "--product", "BTC-USD"])
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == [vid]
    main(["--home", home, "portfolio", "freeze", vid, "--note", "seed portfolio approved"])
    capsys.readouterr()
    main(["--home", home, "portfolio", "show", vid])
    shown = json.loads(capsys.readouterr().out)
    assert shown["state"] == "frozen" and [e["to_state"] for e in shown["events"]] == ["draft", "validated", "frozen"]


def _frozen(paths, store, dataset, monkeypatch, notes=""):
    from quant_research.portfolio.evaluate import validate
    from quant_research.portfolio.seed import seed_version

    _lenient(monkeypatch)
    vs = VersionStore(store)
    vid = vs.put(seed_version(paths, store, "BTC-USD").model_copy(update={"notes": notes}))
    validate(paths, store, vid)
    vs.transition(vid, "frozen", "human", note="test")
    return vid


def test_portfolio_holdout_needs_a_frozen_version(paths, store, dataset):
    from quant_research.validation.holdout import HoldoutError, run_portfolio_holdout

    vid = _seeded(paths, store, dataset)
    with pytest.raises(HoldoutError, match="only a frozen version"):
        run_portfolio_holdout(paths, store, vid)
    assert store.holdout_runs(dataset[1].split_id) == []


def test_portfolio_holdout_records_a_viewing_and_applies_gate_3(paths, store, dataset, monkeypatch):
    from quant_research.validation import holdout

    _desk(paths, store, dataset)
    vid = _frozen(paths, store, dataset, monkeypatch)
    out = holdout.run_portfolio_holdout(paths, store, vid)
    runs = store.holdout_runs(dataset[1].split_id)
    assert [r["candidate_hash"] for r in runs] == [vid] and out["prior_viewings"] == 0
    exp = store.get_experiment(out["holdout_experiment"])
    assert exp["kind"] == "holdout" and exp["segment"] == "holdout" and exp["window_start"] == dataset[1].holdout[0]
    m = exp["metrics"]
    passed = m["sharpe_daily"] >= 0.5 and m["total_return"] > 0
    assert out["passed"] == passed and out["state"] == ("final_evaluated" if passed else "failed_final")
    assert VersionStore(store).get(vid)["evidence"]["holdout_experiment"] == exp["id"]
    again = holdout.run_portfolio_holdout(paths, store, vid)
    assert again["repeat"] and again["holdout_experiment"] == exp["id"]
    assert len(store.holdout_runs(dataset[1].split_id)) == 1


def test_one_version_per_split_per_seven_days(paths, store, dataset, monkeypatch):
    import time

    from quant_research.validation import holdout
    from quant_research.validation.holdout import HoldoutError, run_portfolio_holdout

    _desk(paths, store, dataset)
    monkeypatch.setattr(holdout, "split_viewing_cap", lambda store, split_id: 10)
    first = _frozen(paths, store, dataset, monkeypatch, notes="first")
    second = _frozen(paths, store, dataset, monkeypatch, notes="second")
    run_portfolio_holdout(paths, store, first)
    with pytest.raises(HoldoutError, match="7 days"):
        run_portfolio_holdout(paths, store, second)
    later = run_portfolio_holdout(paths, store, second, now=lambda: time.time() + 8 * 86_400)
    assert later["prior_viewings"] == 1 and store.holdout_overrides(dataset[1].split_id) == []


def test_a_closed_split_refuses_portfolio_holdouts(paths, store, dataset, monkeypatch):
    from quant_research.validation import holdout

    _desk(paths, store, dataset)
    vid = _frozen(paths, store, dataset, monkeypatch)
    store.close_holdout(dataset[1].split_id, "holdout seen")
    monkeypatch.setattr(holdout, "load_holdout_data", lambda *a: pytest.fail("holdout data loaded"))
    with pytest.raises(holdout.HoldoutError, match="is closed \\(holdout seen\\)"):
        holdout.run_portfolio_holdout(paths, store, vid)
    assert store.holdout_runs(dataset[1].split_id) == [] and VersionStore(store).get(vid)["state"] == "frozen"


def test_portfolio_holdout_respects_the_split_cap_and_override(paths, store, dataset, monkeypatch):
    from quant_research.validation import holdout

    _desk(paths, store, dataset)
    monkeypatch.setattr(holdout, "split_viewing_cap", lambda store, split_id: 1)
    store.record_holdout_run(dataset[1].split_id, "some-candidate", "exp-x")
    vid = _frozen(paths, store, dataset, monkeypatch)
    with pytest.raises(holdout.HoldoutError, match="the limit is 1"):
        holdout.run_portfolio_holdout(paths, store, vid)
    out = holdout.run_portfolio_holdout(paths, store, vid, override_reason="human decision")
    assert out["prior_viewings"] == 1
    assert [o["reason"] for o in store.holdout_overrides(dataset[1].split_id)] == ["human decision"]
    assert "contaminated" in store.get_experiment(out["holdout_experiment"])["notes"]


def test_dev_alone_is_a_trial_without_validation_and_marks_one_reference(paths, store, dataset, capsys):
    import json

    from quant_research.cli import main
    from quant_research.portfolio.evaluate import gate1_reference, run_dev

    vid = _seeded(paths, store, dataset)
    split_id = dataset[1].split_id
    trials = store.trial_count(split_id)
    first = run_dev(paths, store, vid)
    vs = VersionStore(store)
    assert store.trial_count(split_id) == trials + 1 and vs.validation_uses(split_id) == 0
    assert first["state"] == "draft" and not first["gate1_reference"]
    assert gate1_reference(store, "BTC-USD", split_id) is None
    main(["--home", str(paths.root), "portfolio", "dev", vid, "--reference"])
    out = json.loads(capsys.readouterr().out)
    assert out["gate1_reference"] and out["dev"]["experiment_id"] == first["dev"]["experiment_id"]
    assert store.trial_count(split_id) == trials + 1 and vs.get(vid)["state"] == "draft"
    assert gate1_reference(store, "BTC-USD", split_id) == vid
    assert run_dev(paths, store, vid, reference=True)["gate1_reference"]
    assert sum(1 for e in vs.events(vid) if e["evidence"] and "gate1_reference" in e["evidence"]) == 1
    other = vs.put(vs.version(vid).model_copy(update={"notes": "another draft"}))
    with pytest.raises(VersionError, match="already gate 1's reference"):
        run_dev(paths, store, other, reference=True)


def test_human_can_replace_the_gate1_reference_when_research_is_off(paths, store, dataset, capsys):
    import json

    from quant_research.cli import main
    from quant_research.desk.config import load_config, save_config
    from quant_research.hypothesis.gates import _reference_dev
    from quant_research.portfolio.evaluate import gate1_reference, run_dev, set_gate1_reference

    first = _seeded(paths, store, dataset)
    vs = VersionStore(store)
    second = vs.put(vs.version(first).model_copy(update={"notes": "lf-allin seed"}))
    run_dev(paths, store, first, reference=True)
    run_dev(paths, store, second)
    split_id = dataset[1].split_id
    assert gate1_reference(store, "BTC-USD", split_id) == first
    with pytest.raises(VersionError, match="research is enabled"):
        set_gate1_reference(paths, store, second, "D4 baseline")
    save_config(paths, load_config(paths).model_copy(update={"research_enabled": False}))
    with pytest.raises(VersionError, match="needs a note"):
        set_gate1_reference(paths, store, second, "  ")
    main(["--home", str(paths.root), "portfolio", "reference", second, "--note", "D4: lf-allin baseline"])
    out = json.loads(capsys.readouterr().out)
    assert out["changed"] and out["previous"] == first and out["gate1_reference"] == second
    assert gate1_reference(store, "BTC-USD", split_id) == second
    assert _reference_dev(store, second)["id"] == out["dev_experiment"]
    again = set_gate1_reference(paths, store, second, "already current")
    assert again["changed"] is False and gate1_reference(store, "BTC-USD", split_id) == second
    superseded = [json.loads(e["evidence"]) for e in vs.events(first)
                  if e["evidence"] and "superseded_by" in json.loads(e["evidence"])]
    assert superseded[-1]["superseded_by"] == second


def test_period_stats_split_a_recorded_run_and_compose():
    from quant_research.data.bars import to_utc
    from quant_research.evaluation.periods import period_stats

    n = 96
    times = to_utc(T0 + HOUR * np.arange(n))
    rng = np.random.default_rng(0)
    equity = 100.0 * np.cumprod(1 + 0.002 * rng.standard_normal(n))
    close = 50.0 * np.cumprod(1 + 0.003 * rng.standard_normal(n))
    eq = pd.DataFrame({"time": times, "equity": equity, "close": close, "exposure": np.r_[np.zeros(48), np.ones(48)]})
    log = pd.DataFrame({"bar_open": times, "level": ["normal"] * 72 + ["halved"] * 24,
                        "yellow": [False] * 60 + [True] * 36})
    cut = T0 + 48 * HOUR
    rows = period_stats(eq, 100.0, [cut, T0 - HOUR, T0 + 10 * n * HOUR], log)
    assert [r["period"] for r in rows] == ["part", "part", "whole"]
    early, late, whole = rows
    assert early["days"] == 2 and late["days"] == 2 and whole["days"] == 4
    assert early["total_return"] == pytest.approx(equity[47] / 100 - 1, abs=1e-4)
    assert late["total_return"] == pytest.approx(equity[-1] / equity[47] - 1, abs=1e-4)
    assert (1 + early["total_return"]) * (1 + late["total_return"]) == pytest.approx(1 + whole["total_return"],
                                                                                     abs=1e-3)
    assert late["buy_and_hold"]["total_return"] == pytest.approx(close[-1] / close[47] - 1, abs=1e-4)
    assert early["exposure"] == 0.0 and late["exposure"] == 1.0
    assert late["ladder_time"] == {"halved": 0.5, "normal": 0.5} and early["yellow_time"] == 0.0
    assert late["yellow_time"] == 0.75 and whole["max_drawdown"] <= min(early["max_drawdown"], late["max_drawdown"])
    assert [r["period"] for r in period_stats(eq, 100.0, [])] == ["part"]


def test_portfolio_periods_command_reads_the_dev_run(paths, store, dataset, capsys):
    import json

    from quant_research.cli import main
    from quant_research.portfolio.evaluate import run_dev

    vid = _seeded(paths, store, dataset)
    with pytest.raises(SystemExit, match="no dev run"):
        main(["--home", str(paths.root), "portfolio", "periods", vid, "--cut", str(T0 + 1000 * HOUR)])
    run_dev(paths, store, vid)
    main(["--home", str(paths.root), "portfolio", "periods", vid, "--cut", str(T0 + 1000 * HOUR)])
    out = json.loads(capsys.readouterr().out)
    assert [p["period"] for p in out["periods"]] == ["part", "part", "whole"]
    assert out["periods"][1]["from"].startswith("2024-02-11") and "ladder_time" in out["periods"][0]
    assert [p["period"] for p in out["periods_no_ladder"]] == ["part", "part", "whole"]
    assert "ladder_time" not in out["periods_no_ladder"][0]
