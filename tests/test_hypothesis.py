import json

import numpy as np
import pandas as pd
import pytest

from quant_research.hypothesis.model import Hypothesis
from quant_research.hypothesis.store import HypothesisError, HypothesisStore
from quant_research.library.catalog import Family, LibraryError
from quant_research.library.store import LibraryStore
from quant_research.portfolio.version import VersionError, VersionStore
from tests.test_portfolio import _desk


def _reference(paths, store, dataset):
    from quant_research.portfolio.evaluate import run_dev
    from quant_research.portfolio.seed import define_seed

    _desk(paths, store, dataset)
    vid = define_seed(paths, store, "BTC-USD")
    run_dev(paths, store, vid, reference=True)
    return vid


def _hyp(**kw):
    body = {"product": "BTC-USD", "family": "tsmom", "prior": "Short lookbacks hedge the slow sleeves.",
            "citations": [{"kind": "published", "ref": "Moskowitz, Ooi, Pedersen (2012)"}],
            "variants": [{"strategy_ref": "template:momentum", "parameters": {"lookback": 48}}]}
    return Hypothesis.model_validate(body | kw)


# -- C1 registration ---------------------------------------------------------------------------------------

def test_registration_fixes_split_reference_and_normalised_variants(paths, store, dataset):
    ref = _reference(paths, store, dataset)
    hs = HypothesisStore(store)
    row = hs.register(paths, _hyp(), "human", "first")
    assert row["id"].startswith("hy-") and row["state"] == "registered"
    assert row["split_id"] == dataset[1].split_id and row["reference_id"] == ref
    assert row["body"]["variants"][0]["parameters"] == {"lookback": 48, "threshold": 0.0}
    assert [e["event"] for e in hs.events(row["id"])] == ["registered"]
    with pytest.raises(HypothesisError, match="already registered"):
        hs.register(paths, _hyp(), "human", "again")


@pytest.mark.parametrize("change, message", [
    ({"family": "nope"}, "unknown family"),
    ({"family": "vol_overlay"}, "has no sleeve"),
    ({"variants": [{"strategy_ref": "template:nope"}]}, "variant 1"),
    ({"variants": [{"strategy_ref": "template:momentum", "parameters": {"lookback": -1}}]}, "variant 1"),
    ({"variants": [{"strategy_ref": "plugin:" + "0" * 64}]}, "not registered"),
    ({"variants": [{"strategy_ref": "template:momentum", "parameters": {"lookback": 48}},
                   {"strategy_ref": "template:momentum", "parameters": {"lookback": 48, "threshold": 0.0}}]},
     "repeats"),
    ({"citations": [{"kind": "experiment", "ref": "exp-missing"}]}, "does not exist"),
])
def test_registration_refusals(paths, store, dataset, change, message):
    _reference(paths, store, dataset)
    with pytest.raises(HypothesisError, match=message):
        HypothesisStore(store).register(paths, _hyp(**change), "human", "x")


def test_registration_needs_a_mandate_and_a_reference(paths, store, dataset):
    from quant_research.portfolio.seed import define_seed

    with pytest.raises(HypothesisError, match="no desk mandate"):
        HypothesisStore(store).register(paths, _hyp(), "human", "x")
    _desk(paths, store, dataset)
    define_seed(paths, store, "BTC-USD")
    with pytest.raises(HypothesisError, match="no gate 1 reference"):
        HypothesisStore(store).register(paths, _hyp(), "human", "x")


def test_model_limits_and_one_registration_per_campaign(paths, store, dataset):
    v = {"strategy_ref": "template:momentum", "parameters": {"lookback": 24}}
    with pytest.raises(ValueError):
        _hyp(variants=[v | {"parameters": {"lookback": n}} for n in (1, 2, 3, 4)])
    with pytest.raises(ValueError):
        _hyp(variants=[])
    with pytest.raises(ValueError):
        _hyp(citations=[])
    _reference(paths, store, dataset)
    hs = HypothesisStore(store)
    hs.register(paths, _hyp(campaign_id="cmp-1"), "controller")
    with pytest.raises(HypothesisError, match="already registered a hypothesis"):
        hs.register(paths, _hyp(campaign_id="cmp-1", variants=[v]), "controller")


def test_experiment_citations_must_be_dev_on_the_split(paths, store, dataset):
    from quant_research.portfolio.evaluate import run_segment

    ref = _reference(paths, store, dataset)
    dev = store.query("SELECT id FROM experiments WHERE kind='portfolio'")[0]["id"]
    val = run_segment(paths, store, ref, "validation")["id"]
    hs = HypothesisStore(store)
    with pytest.raises(HypothesisError, match="not a dev experiment"):
        hs.register(paths, _hyp(citations=[{"kind": "experiment", "ref": val}]), "human")
    assert hs.register(paths, _hyp(citations=[{"kind": "experiment", "ref": dev}]), "human")["state"] == "registered"


# -- C3 library ----------------------------------------------------------------------------------------

def test_candidate_entries_are_idempotent_and_stay_out_of_the_seed_portfolio(paths, store, dataset):
    from quant_research.portfolio.seed import seed_version

    _desk(paths, store, dataset)
    before = seed_version(paths, store, "BTC-USD").version_id()
    lib = LibraryStore(paths.db)
    try:
        eid = lib.add_candidate("BTC-USD", "tsmom", "template:momentum", {"lookback": 48}, "hypothesis:hy-x")
        assert eid.startswith("btc-tsmom-h")
        assert lib.add_candidate("BTC-USD", "tsmom", "template:momentum", {"lookback": 48, "threshold": 0.0},
                                 "hypothesis:hy-y") == eid
        row = lib.entry(eid)
        assert row["status"] == "candidate" and row["source"] == "hypothesis:hy-x" and row["kind"] == "member"
        with pytest.raises(LibraryError, match="not a sleeve family"):
            lib.add_candidate("BTC-USD", "vol_overlay", "template:momentum", {"lookback": 48}, "x")
    finally:
        lib.close()
    assert seed_version(paths, store, "BTC-USD").version_id() == before


def test_family_add_is_the_humans_and_unique(paths, store, dataset, capsys):
    from quant_research.cli import main

    _desk(paths, store, dataset)
    home = str(paths.root)
    main(["--home", home, "library", "family", "add", "--id", "carry", "--name", "Carry", "--thesis", "funding",
          "--note", "confirmed"])
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "carry" and out["sleeve"] == 1 and out["source"].startswith("human")
    with pytest.raises(SystemExit, match="already exists"):
        main(["--home", home, "library", "family", "add", "--id", "carry", "--name", "C", "--thesis", "t",
              "--note", "again"])
    lib = LibraryStore(paths.db)
    try:
        with pytest.raises(LibraryError, match="needs a note"):
            lib.add_family(Family(id="x", name="x", thesis="x", sleeve=True), " ")
    finally:
        lib.close()


# -- C4 gate 1 -----------------------------------------------------------------------------------------

def _days(n):
    return pd.date_range("2020-06-01", periods=n, freq="D")


def test_gate1_statistic_fails_a_levered_copy_and_passes_independent_drift():
    from quant_research.hypothesis.gates import _regression

    rng = np.random.default_rng(0)
    idx = _days(1500)
    p = pd.Series(0.0008 + 0.03 * rng.standard_normal(len(idx)), index=idx)
    copy = pd.DataFrame({"c": 0.5 * p + 0.002 * rng.standard_normal(len(idx)), "p": p})
    assert not _regression(copy, 1)["passed"] and _regression(copy, 1)["beta"] == pytest.approx(0.5, abs=0.02)
    own = pd.DataFrame({"c": 0.002 + 0.02 * rng.standard_normal(len(idx)), "p": p})
    one, three = _regression(own, 1), _regression(own, 3)
    assert one["passed"] and one["k"] == 1 and three["k"] == 3 and three["statistic"] < one["statistic"]
    periods = one["periods"]
    assert [p["from"] for p in periods] == ["2020-06-01", "2021-01-01", "2020-06-01"]
    assert periods[0]["days"] + periods[1]["days"] == periods[2]["days"] == len(idx)


def test_gate1_runs_each_variant_once_as_a_trial_and_records_the_verdict(paths, store, dataset, monkeypatch):
    from quant_research.hypothesis import gates

    _reference(paths, store, dataset)
    hs = HypothesisStore(store)
    h = hs.register(paths, _hyp(variants=[{"strategy_ref": "template:momentum", "parameters": {"lookback": 48}},
                                          {"strategy_ref": "template:buy_and_hold"}]), "human")
    split_id = dataset[1].split_id
    trials = store.trial_count(split_id)
    ks = []
    real = gates.appraisal_test

    def fake(c, p, k):
        ks.append(k)
        out = real(c, p, k)
        return out | {"passed": len(ks) == 2}
    monkeypatch.setattr(gates, "appraisal_test", fake)
    g1 = gates.gate1(paths, store, h["id"])
    assert ks == [2, 2] and store.trial_count(split_id) == trials + 2
    assert g1["passed"] and g1["best_variant"] == 1 and g1["k"] == 2
    runs = store.query("SELECT kind, segment FROM experiments WHERE kind='registered'")
    assert runs == [{"kind": "registered", "segment": "dev"}] * 2
    assert all(v["days"] >= gates.MIN_DAYS and "periods" in v for v in g1["variants"])
    assert gates.gate1(paths, store, h["id"]) == g1 and store.trial_count(split_id) == trials + 2
    assert hs.state(h["id"]) == "registered"


def test_gate1_failure_ends_the_hypothesis(paths, store, dataset, monkeypatch):
    from quant_research.hypothesis import gates

    _reference(paths, store, dataset)
    h = HypothesisStore(store).register(paths, _hyp(), "human")
    monkeypatch.setattr(gates, "appraisal_test", lambda c, p, k: {"beta": 0.0, "alpha_daily": 0.0,
                                                                   "appraisal_ratio": 0.1, "k": k,
                                                                   "statistic": 0.2, "passed": False})
    out = gates.run_gates(paths, store, h["id"])
    assert out["state"] == "gate1_failed" and out["gate2"] is None
    with pytest.raises(HypothesisError, match="no variant that passed"):
        gates.gate2(paths, store, h["id"])
    assert gates.verdict(store, h["id"]) == {"hypothesis_id": h["id"], "state": "gate1_failed", "failed_gate": 1}


def test_gate1_reads_the_reference_without_the_ladder(paths, store, dataset):
    from quant_research.hypothesis.gates import _reference_dev, _returns
    from quant_research.memory.returns import daily_returns

    ref = _reference(paths, store, dataset)
    row = _reference_dev(store, ref)
    nl = pd.read_parquet(paths.results / row["id"] / "equity_no_ladder.parquet")
    expected = daily_returns(nl, row["engine_config"]["initial_cash"]).to_numpy()
    assert _returns(paths, row).to_numpy() == pytest.approx(expected)


def test_marginal_needs_no_registration(paths, store, dataset):
    from quant_research.hypothesis.gates import marginal

    _reference(paths, store, dataset)
    dev = store.query("SELECT id FROM experiments WHERE kind='portfolio'")[0]["id"]
    out = marginal(paths, store, dev)
    assert out["beta"] == pytest.approx(1.0) and "passed" not in out and out["periods"]
    with pytest.raises(HypothesisError, match="not a completed dev"):
        marginal(paths, store, "exp-none")


# -- C5 gate 2, C6 admitted -------------------------------------------------------------------------------

def test_gate2_rule_each_condition_alone():
    from quant_research.hypothesis.gates import gate2_checks

    ref = {"sharpe": 1.0, "max_drawdown": -0.20, "turnover": 10.0}
    ok = {"sharpe": 1.0, "max_drawdown": -0.22, "turnover": 12.5}
    assert all(gate2_checks(ref, ok).values())
    assert all(gate2_checks(ref, ok | {"sharpe": 0.95}).values())  # A13.9 slack 0.05
    assert gate2_checks(ref, ok | {"sharpe": 0.94}) == {"sharpe_not_lower": False, "drawdown_within_2_points": True,
                                                       "turnover_within_25_percent": True}
    assert not gate2_checks(ref, ok | {"max_drawdown": -0.2201})["drawdown_within_2_points"]
    assert not gate2_checks(ref, ok | {"turnover": 12.51})["turnover_within_25_percent"]


def _passed_gate1(paths, store, dataset, monkeypatch):
    from quant_research.hypothesis import gates

    _reference(paths, store, dataset)
    h = HypothesisStore(store).register(paths, _hyp(), "human")
    real = gates.appraisal_test
    monkeypatch.setattr(gates, "appraisal_test", lambda c, p, k: real(c, p, k) | {"passed": True})
    return h["id"]


@pytest.mark.parametrize("passing", [True, False])
def test_gate2_builds_the_candidate_counts_validation_and_sets_the_state(paths, store, dataset, monkeypatch, passing):
    from quant_research.hypothesis import gates

    hid = _passed_gate1(paths, store, dataset, monkeypatch)
    monkeypatch.setattr(gates, "gate2_checks", lambda r, c: {"sharpe_not_lower": passing,
                                                              "drawdown_within_2_points": True,
                                                              "turnover_within_25_percent": True})
    split_id = dataset[1].split_id
    vs = VersionStore(store)
    out = gates.run_gates(paths, store, hid)
    g2 = out["gate2"]
    assert out["state"] == ("admitted" if passing else "gate2_failed") and g2["passed"] is passing
    cand = vs.version(g2["version_id"])
    ref = vs.version(g2["reference_id"])
    assert cand.parent_id == g2["reference_id"] and len(cand.members) == len(ref.members) + 1
    new = cand.members[-1]
    assert new.family == "tsmom" and new.status == "member" and new.entry_id.startswith("btc-tsmom-h")
    assert vs.get(g2["version_id"])["state"] == ("admitted" if passing else "failed_validation")
    assert vs.validation_uses(split_id) == 2
    assert set(g2["reference"]) == {"sharpe", "max_drawdown", "turnover"}
    with pytest.raises(HypothesisError, match="already ran"):
        gates.gate2(paths, store, hid)
    assert gates.verdict(store, hid)["failed_gate"] == (None if passing else 2)
    assert "reference" not in gates.verdict(store, hid)


def test_admitted_versions_cannot_be_frozen_or_approved(paths, store, dataset, monkeypatch):
    from quant_research.hypothesis import gates
    from quant_research.paper.fleet import load_roster
    from quant_research.portfolio.paper import approve, start_shadow

    hid = _passed_gate1(paths, store, dataset, monkeypatch)
    monkeypatch.setattr(gates, "gate2_checks", lambda r, c: {"ok": True})
    vid = gates.run_gates(paths, store, hid)["gate2"]["version_id"]
    vs = VersionStore(store)
    assert vs.get(vid)["state"] == "admitted"
    with pytest.raises(VersionError):
        vs.transition(vid, "frozen", "human")
    with pytest.raises(VersionError):
        approve(paths, store, vid, "x")
    out = start_shadow(paths, store, vid, "forward")
    assert out["roster_name"].startswith("shadow-btc-usd-admitted-")
    entry = next(e for e in load_roster(paths.desk / "roster.json").entries if e.name == out["roster_name"])
    assert entry.shadow and entry.portfolio_version == vid
    lines = gates.report_lines(store, "2027-10-12")
    assert any(hid in line and "admitted" in line and vid in line for line in lines)
    assert any("2027-10-12" in line for line in lines)


def test_super_authorization_puts_admitted_on_paper_now(paths, store, dataset, monkeypatch):
    from quant_research.desk import promotion
    from quant_research.hypothesis import gates
    from quant_research.paper.fleet import load_roster
    from quant_research.portfolio.paper import ensure_admitted_paper, start_admitted_paper
    from quant_research.research import authorization as auth_mod

    hid = _passed_gate1(paths, store, dataset, monkeypatch)
    monkeypatch.setattr(gates, "gate2_checks", lambda r, c: {"ok": True})
    vid = gates.run_gates(paths, store, hid)["gate2"]["version_id"]
    with pytest.raises(VersionError, match="super authorization"):
        start_admitted_paper(paths, store, vid)
    auth = auth_mod.create(["BTC-USD"], 2, days=7, auto_paper_admitted=True)
    auth_mod.save(promotion.authorization_path(paths), auth)
    out = start_admitted_paper(paths, store, vid, authorization=auth)
    assert out["shadow"] is False and out["roster_name"].startswith("paper-btc-usd-admitted-")
    assert VersionStore(store).get(vid)["state"] == "admitted"
    entry = next(e for e in load_roster(paths.desk / "roster.json").entries if e.name == out["roster_name"])
    assert not entry.shadow and entry.portfolio_version == vid
    again = start_admitted_paper(paths, store, vid, authorization=auth)
    assert again.get("already") and again["roster_name"] == out["roster_name"]
    assert ensure_admitted_paper(paths, store)[0].get("already")


def test_super_authorization_upgrades_an_admitted_shadow(paths, store, dataset, monkeypatch):
    from quant_research.desk import promotion
    from quant_research.hypothesis import gates
    from quant_research.paper.fleet import load_roster
    from quant_research.portfolio.paper import start_admitted_paper, start_shadow
    from quant_research.research import authorization as auth_mod

    hid = _passed_gate1(paths, store, dataset, monkeypatch)
    monkeypatch.setattr(gates, "gate2_checks", lambda r, c: {"ok": True})
    vid = gates.run_gates(paths, store, hid)["gate2"]["version_id"]
    shadow = start_shadow(paths, store, vid, "forward")
    auth = auth_mod.create(["BTC-USD"], 2, days=7, auto_paper_admitted=True)
    auth_mod.save(promotion.authorization_path(paths), auth)
    out = start_admitted_paper(paths, store, vid, authorization=auth)
    assert out.get("upgraded") and out["roster_name"] == shadow["roster_name"]
    entry = next(e for e in load_roster(paths.desk / "roster.json").entries if e.portfolio_version == vid)
    assert not entry.shadow


def test_hypothesis_commands_end_to_end(paths, store, dataset, capsys, monkeypatch, tmp_path):
    from quant_research.cli import main
    from quant_research.hypothesis import gates

    _reference(paths, store, dataset)
    home = str(paths.root)
    f = tmp_path / "h.yaml"
    f.write_text("product: BTC-USD\nfamily: ewma_trend\nprior: a prior\n"
                 "citations: [{kind: published, ref: 'Hurst, Ooi, Pedersen (2017)'}]\n"
                 "variants: [{strategy_ref: 'template:sma_cross', parameters: {fast: 24, slow: 96}}]\n")
    with pytest.raises(SystemExit):
        main(["--home", home, "hypothesis", "register", "--file", str(f)])
    main(["--home", home, "hypothesis", "register", "--file", str(f), "--note", "human idea"])
    hid = json.loads(capsys.readouterr().out)["id"]
    monkeypatch.setattr(gates, "appraisal_test", lambda c, p, k: {"beta": 0.0, "alpha_daily": 0.0,
                                                                   "appraisal_ratio": 0.0, "k": k,
                                                                   "statistic": 0.5, "passed": False})
    main(["--home", home, "hypothesis", "test", hid, "--sandbox", "none"])
    assert json.loads(capsys.readouterr().out)["state"] == "gate1_failed"
    main(["--home", home, "hypothesis", "list", "--state", "gate1_failed"])
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == [hid]
    main(["--home", home, "hypothesis", "show", hid])
    assert [e["event"] for e in json.loads(capsys.readouterr().out)["events"]] == ["registered", "gate1"]
    with pytest.raises(SystemExit, match="unknown hypothesis"):
        main(["--home", home, "hypothesis", "show", "hy-none"])


def test_plugin_variants_are_frozen_at_registration(paths, store, dataset, tmp_path):
    from quant_research.hypothesis.commands import load_file

    src = tmp_path / "p.py"
    src.write_text("def generate_exposures(history, parameters):\n    return [1.0] * len(history)\n")
    f = tmp_path / "h.yaml"
    f.write_text("product: BTC-USD\nfamily: tsmom\nprior: p\ncitations: [{kind: published, ref: r}]\n"
                 "variants: [{plugin_file: p.py}]\n")
    body = load_file(paths, f)
    ref = body["variants"][0]["strategy_ref"]
    assert ref.startswith("plugin:") and "plugin_file" not in body["variants"][0]
    src.write_text("def generate_exposures(history, parameters):\n    return [0.0] * len(history)\n")
    assert (paths.artifacts / "strategies" / f"{ref.split(':')[1]}.py").read_text().count("1.0") == 1
