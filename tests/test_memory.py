"""Research memory (contracts v2 appendix A): return archive, residuals, clusters, families, conclusions, archives."""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
import pytest

from quant_research.experiments.gatekeeper import GateContext
from quant_research.experiments.runner import ExperimentRequest, run_experiment
from quant_research.memory.returns import ReturnArchive, backfill, daily_returns


def _req(dataset, **kw):
    manifest, split = dataset
    base = {"strategy_ref": "template:sma_cross", "parameters": {"fast": 10, "slow": 50},
            "dataset_id": manifest["dataset_id"], "split_id": split.split_id}
    return ExperimentRequest(**{**base, **kw})


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- daily returns (A1) ----------------------------------------------------------------------------------------

def test_daily_returns_use_the_last_bar_of_each_utc_day():
    times = pd.date_range("2021-01-01", periods=50, freq="h", tz="UTC")
    eq = pd.DataFrame({"time": times, "equity": 10_001.0 + np.arange(50)})
    r = daily_returns(eq, 10_000.0)
    assert list(r.index.strftime("%Y-%m-%d")) == ["2021-01-01", "2021-01-02", "2021-01-03"]
    assert r.iloc[0] == pytest.approx(10_024 / 10_000 - 1)
    assert r.iloc[1] == pytest.approx(10_048 / 10_024 - 1)
    assert r.iloc[2] == pytest.approx(10_050 / 10_048 - 1)


def test_the_dev_window_gives_1267_days_with_a_partial_last_day():
    times = pd.to_datetime(1609459200 + 3600 * np.arange(30_402), unit="s", utc=True)
    r = daily_returns(pd.DataFrame({"time": times, "equity": 10_000.0}), 10_000.0)
    assert len(r) == 1267 and str(r.index[-1].date()) == "2024-06-20"
    assert (times.normalize() == times[-1].normalize()).sum() == 18


# -- archive and backfill (A1) ---------------------------------------------------------------------------------

def _trials(paths, store, dataset, tmp_path):
    ok = [run_experiment(paths, store, _req(dataset, parameters={"fast": f, "slow": 60})).experiment_id
          for f in (5, 10, 20)]
    rejected = run_experiment(paths, store, _req(dataset, parameters={"fast": 50, "slow": 10})).experiment_id
    stop = tmp_path / "STOP"
    stop.write_text("")
    blocked = run_experiment(paths, store, _req(dataset, parameters={"fast": 7, "slow": 60}),
                             gate=GateContext(stop_file=stop))
    assert blocked.status == "blocked"
    run_experiment(paths, store, _req(dataset, segment="validation"))
    run_experiment(paths, store, _req(dataset, parameters={"fast": 5, "slow": 60}), kind="sensitivity")
    return ok, rejected


def test_new_dev_trials_are_archived_when_they_finish(paths, store, dataset, tmp_path):
    ok, rejected = _trials(paths, store, dataset, tmp_path)
    split = dataset[1].split_id
    arc = ReturnArchive(paths, store)
    assert list(arc.load(split).columns) == sorted(ok)
    index = {r["experiment_id"]: r for r in arc.index(split)}
    assert set(index) == {*ok, rejected}
    assert index[rejected]["no_returns"] == 1 and index[ok[0]]["no_returns"] == 0
    assert index[ok[0]]["n_days"] == len(arc.load(split))


def test_backfill_rebuilds_the_same_archive_and_is_idempotent(paths, store, dataset, tmp_path):
    ok, rejected = _trials(paths, store, dataset, tmp_path)
    split = dataset[1].split_id
    arc = ReturnArchive(paths, store)
    before, rows = _sha(arc.path(split)), arc.index(split)
    arc.path(split).unlink()
    store.execute("DELETE FROM return_index")
    counts = backfill(paths, store)
    assert counts[split] == {"added": 3, "no_returns": 1, "missing_equity": 0, "present": 0}
    assert _sha(arc.path(split)) == before
    assert [r["experiment_id"] for r in arc.index(split)] == [r["experiment_id"] for r in rows]
    assert backfill(paths, store)[split]["present"] == 4
    assert _sha(arc.path(split)) == before


# -- residuals (A2) --------------------------------------------------------------------------------------------

DAYS = pd.date_range("2021-01-01", periods=1267, freq="D", name="day")


def _market(seed=0):
    return pd.Series(np.random.default_rng(seed).normal(0.001, 0.035, len(DAYS)), index=DAYS)


def test_market_returns_are_close_to_close_on_the_day_grid():
    from quant_research.memory.residuals import market_returns

    t = pd.date_range("2021-01-01", periods=48, freq="h", tz="UTC")
    bars = pd.DataFrame({"bar_open_time": t, "open": 100.0 + np.arange(48), "close": 101.0 + np.arange(48)})
    m = market_returns(bars, (int(t[0].timestamp()), int(t[-1].timestamp()) + 3600))
    assert m.iloc[0] == pytest.approx(124 / 100 - 1) and m.iloc[1] == pytest.approx(148 / 124 - 1)


def test_residuals_remove_the_market_component():
    from quant_research.memory.residuals import residuals

    m = _market()
    noise = np.random.default_rng(1).normal(0, 0.01, len(DAYS))
    resid, flags = residuals(pd.DataFrame({"a": 0.0002 + 0.6 * m + noise}), m, winsorise=False)
    assert flags.loc["a", "beta"] == pytest.approx(0.6, abs=0.02)
    assert abs(np.corrcoef(resid["a"], m)[0, 1]) < 1e-9


def test_winsorising_clips_at_three_standard_deviations_once():
    from quant_research.memory.residuals import residuals

    m = _market()
    r = np.random.default_rng(2).normal(0, 0.01, len(DAYS))
    r[10] = 0.5
    raw, _ = residuals(pd.DataFrame({"a": r}, index=DAYS), m, winsorise=False)
    w, flags = residuals(pd.DataFrame({"a": r}, index=DAYS), m)
    eps = raw["a"]
    assert w["a"].max() == pytest.approx(eps.mean() + 3 * eps.std(ddof=1))
    assert (w["a"] == eps).sum() == len(DAYS) - 1


def test_flat_and_passive_series_are_flagged():
    from quant_research.memory.residuals import residuals

    m = _market()
    trend = np.where(np.random.default_rng(3).random(len(DAYS)) > 0.5, m, 0.0)
    _, flags = residuals(pd.DataFrame({"cash": 0.0, "hold": m, "trend": trend}), m)
    assert flags["flat"].tolist() == [True, False, False]
    assert flags["passive"].tolist() == [False, True, False]


# -- clustering and K (A3) -------------------------------------------------------------------------------------

def _planted(seed=0):
    """3 groups of 10 trials (residual correlation ~0.8 within, ~0 across), 5 independent, 1 flat, 1 passive."""
    rng = np.random.default_rng(seed + 100)  # not the market's stream, or group 0's factor would be the market
    m = _market(seed)
    cols = {}
    for g in range(3):
        f = rng.normal(0, 0.01, len(DAYS))
        for j in range(10):
            cols[f"exp-g{g}-{j:02d}"] = 0.5 * m + f + rng.normal(0, 0.005, len(DAYS))
    for j in range(5):
        cols[f"exp-solo-{j}"] = 0.3 * m + rng.normal(0, 0.01, len(DAYS))
    cols["exp-zflat"] = np.zeros(len(DAYS))
    cols["exp-zhold"] = m.to_numpy()
    return pd.DataFrame(cols, index=DAYS), m


def test_planted_clusters_are_recovered_and_k_counts_special_trials_once():
    from quant_research.memory.clusters import cluster
    from quant_research.memory.residuals import residuals

    returns, m = _planted()
    labels, k = cluster(*residuals(returns, m), no_returns=["exp-failed-1", "exp-failed-2"])
    assert k == {"0.3": 3 + 5 + 1 + 1 + 2, "0.5": 12, "0.7": 12}
    lab = labels["0.7"]
    assert {lab[f"exp-g1-{j:02d}"] for j in range(10)} == {"exp-g1-00"}
    assert lab["exp-zflat"] == "flat" and lab["exp-zhold"] == "passive" and lab["exp-failed-1"] == "exp-failed-1"


def test_labels_are_canonical_and_recomputation_is_identical():
    from quant_research.memory.clusters import cluster, input_hash
    from quant_research.memory.residuals import residuals

    returns, m = _planted()
    shuffled = returns[list(np.random.default_rng(9).permutation(returns.columns))]
    assert cluster(*residuals(returns, m), []) == cluster(*residuals(shuffled, m), [])
    assert input_hash(returns, [], True) == input_hash(shuffled, [], True)


def test_clustering_is_recorded_once_per_input(paths, store, dataset, tmp_path):
    from quant_research.memory import clusters

    ok, rejected = _trials(paths, store, dataset, tmp_path)
    split = dataset[1].split_id
    c = clusters.compute(paths, store, split)
    assert c.counts["trials"] == 4 and c.counts["no_returns"] == 1
    cid = clusters.record(store, c)
    assert clusters.record(store, clusters.compute(paths, store, split)) == cid
    assert clusters.effective_k(store, split) == c.k["0.7"] >= 2
    assert set(clusters.members(store, cid, 0.7)) == {*ok, rejected}


# -- family tags and cluster mapping (A12 point 4) -------------------------------------------------------------

def _ledger(paths, sessions: dict[str, tuple[str, str]]):
    import json

    paths.desk.mkdir(parents=True, exist_ok=True)
    (paths.desk / "research_ledger.jsonl").write_text("".join(
        json.dumps({"session_id": sid, "product": p, "topic": t, "run_id": f"run-{sid}"}) + "\n"
        for sid, (p, t) in sessions.items()))


def test_legacy_topics_map_to_family_tags(paths, store, dataset):
    from quant_research.memory.families import tag_trials

    _ledger(paths, {"sess-v": ("BTC-USD", "volatility_filter"), "sess-t": ("BTC-USD", "trend"),
                    "sess-m": ("BTC-USD", "mean_reversion")})
    ids = {sid: run_experiment(paths, store, _req(dataset, parameters={"fast": f, "slow": 60}),
                               session_id=sid).experiment_id
           for sid, f in (("sess-v", 5), ("sess-t", 10), ("sess-m", 20))}
    tag_trials(paths, store)
    rows = {r["experiment_id"]: r for r in store.query("SELECT * FROM return_index")}
    assert (rows[ids["sess-v"]]["family_tag"], rows[ids["sess-v"]]["tag_source"]) == \
        ("vol_overlay", "topic:volatility_filter")
    assert (rows[ids["sess-t"]]["family_tag"], rows[ids["sess-t"]]["tag_source"]) == (None, "topic:trend")
    assert rows[ids["sess-m"]]["family_tag"] == "short_term_reversal"


def _fake_clustering(store, groups: dict[str, list[tuple[str, str, str | None]]]):
    """groups: label -> [(experiment id, kind, family tag)]; writes return_index and cluster_members rows."""
    from quant_research.memory.schema import ensure

    ensure(store)
    for label, members in groups.items():
        for exp_id, kind, tag in members:
            store.execute("INSERT INTO return_index (experiment_id, split_id, kind, family_tag, no_returns, sharpe) "
                          "VALUES (?,?,?,?,0,0.1)", (exp_id, "s", kind, tag))
            store.execute("INSERT INTO cluster_members VALUES (?,?,?,?)", ("clu-x", 0.3, exp_id, label))


def test_seeds_decide_a_cluster_and_ties_go_to_the_lowest_id_seed(store):
    from quant_research.memory.families import map_clusters

    _fake_clustering(store, {
        "a": [("a1", "seed", "tsmom"), ("a2", "seed", "ewma_trend"), ("a3", "backtest", "vol_overlay"),
              ("a4", "backtest", "vol_overlay"), ("a5", "backtest", "vol_overlay")],
        "b": [("b1", "seed", "ewma_trend"), ("b2", "seed", "tsmom"), ("b3", "seed", "tsmom")]})
    m = map_clusters(store, "clu-x")
    assert (m["a"]["family"], m["a"]["basis"]) == ("tsmom", "seed")  # seeds outrank tags; tie -> lowest id
    assert m["b"]["family"] == "tsmom"  # majority of seeds


def test_without_seeds_a_family_needs_more_than_half_of_the_cluster(store):
    from quant_research.memory.families import map_clusters

    _fake_clustering(store, {
        "c": [("c1", "backtest", "vol_overlay")] + [(f"c{i}", "backtest", None) for i in range(2, 10)],
        "d": [(f"d{i}", "backtest", "vol_overlay") for i in range(3)] + [("d9", "backtest", None)]})
    m = map_clusters(store, "clu-x")
    assert m["c"]["family"] is None and m["d"]["family"] == "vol_overlay"


def test_an_unmapped_cluster_becomes_one_proposal(paths, store, dataset):
    from quant_research.memory import clusters
    from quant_research.memory.families import map_clusters, proposals, propose

    for f in range(3, 9):
        run_experiment(paths, store, _req(dataset, parameters={"fast": f, "slow": 60}))
    split = dataset[1].split_id
    cid = clusters.record(store, clusters.compute(paths, store, split))
    mapping = map_clusters(store, cid)
    big = [m for m in mapping.values() if m["size"] >= 5]
    assert big and all(m["family"] is None for m in big)
    created = propose(store, split, cid, mapping)
    assert len(created) == len(big) and propose(store, split, cid, mapping) == []
    body = proposals(store, split)[0]["body"]
    assert body["representative"]["strategy_ref"] == "template:sma_cross" and body["size"] >= 5


# -- seed characterisation (A7) --------------------------------------------------------------------------------

def _desk(paths, store, dataset):
    from quant_research.desk.config import DeskConfig, save_config
    from tests.conftest import make_mandate

    m = make_mandate(dataset)
    store.put_mandate(m.mandate_id(), m.model_dump(mode="json"))
    save_config(paths, DeskConfig(mandates={"BTC-USD": m.mandate_id()}))


def test_characterise_runs_each_seed_instance_once_on_dev(paths, store, dataset):
    from quant_research.memory.characterise import characterise, seed_runs
    from quant_research.memory.families import tag_trials

    _desk(paths, store, dataset)
    split = dataset[1].split_id
    out = characterise(paths, store, entries={"tsmom-1w", "turtle-s1"})
    assert [o["instance"] for o in out] == ["btc-tsmom-1w", "btc-turtle-s1"]
    assert all(o["status"] == "completed" for o in out)
    assert characterise(paths, store, entries={"tsmom-1w", "turtle-s1"}) == []
    assert set(seed_runs(store, split)) == {"btc-tsmom-1w", "btc-turtle-s1"}
    assert store.trial_count(split_id=split) == 2
    tag_trials(paths, store)
    tags = {r["tag_source"]: r["family_tag"] for r in store.query("SELECT * FROM return_index")}
    assert tags == {"seed:tsmom-1w": "tsmom", "seed:turtle-s1": "channel_breakout"}


# -- session conclusions ---------------------------------------------------------------------------------------

def _session_with_report(paths, store, dataset, sharpe_offset=0.0):
    import json
    from pathlib import Path

    from quant_research.controller.agent import ManualRunner
    from quant_research.controller.controller import ResearchController, SessionConfig
    from quant_research.strategy.sandbox import SandboxConfig
    from tests.conftest import make_mandate

    sid = ResearchController(paths, store, ManualRunner()).create_session(
        SessionConfig(mandate=make_mandate(dataset), sandbox=SandboxConfig(isolation="subprocess")))
    res = run_experiment(paths, store, _req(dataset), session_id=sid)
    doc = {"research_id": "r", "task_id": "desk-volatility_filter", "hypothesis": "h", "preregistered_plan": {},
           "experiment_ids": [res.experiment_id], "strategy_artifacts": [], "decision": "reject",
           "results": [{"experiment_id": res.experiment_id,
                        "metrics": {"sharpe": res.metrics["sharpe"] + sharpe_offset}}],
           "limitations": ["dev only"], "sources": [], "unresolved": ["why"], "final_artifact": None}
    ws = Path(store.get_session(sid)["workspace"])
    (ws / "research.json").write_text(json.dumps(doc))
    return sid, ws


def test_import_conclusions_is_append_only_and_idempotent(paths, store, dataset):
    from quant_research.memory.conclusions import conclusions, import_conclusions

    good, ws = _session_with_report(paths, store, dataset)
    bad, _ = _session_with_report(paths, store, dataset, sharpe_offset=0.5)
    _ledger(paths, {good: ("BTC-USD", "volatility_filter"), bad: ("BTC-USD", "trend")})
    assert import_conclusions(paths, store) == {"imported": 2, "present": 0, "citation_failures": 1}
    rows = {r["session_id"]: r for r in conclusions(store)}
    assert rows[good]["citations_ok"] == 1 and rows[good]["family_id"] == "vol_overlay"
    assert rows[good]["body"]["decision"] == "reject" and rows[good]["body"]["topic"] == "volatility_filter"
    assert rows[bad]["citations_ok"] == 0 and "sharpe" in rows[bad]["problems"][0]
    (ws / "research.json").write_text("{}")
    assert import_conclusions(paths, store) == {"imported": 0, "present": 2, "citation_failures": 0}
    assert {r["session_id"]: r["body"] for r in conclusions(store)}[good]["hypothesis"] == "h"


# -- family archives -------------------------------------------------------------------------------------------

def _memory_home(paths, store, dataset):
    from quant_research.memory import clusters
    from quant_research.memory.characterise import characterise
    from quant_research.memory.families import tag_trials

    _desk(paths, store, dataset)
    characterise(paths, store, entries={"tsmom-1w", "tsmom-1m", "turtle-s1"})
    for sid, f in (("sess-a", 5), ("sess-b", 10)):
        run_experiment(paths, store, _req(dataset, parameters={"fast": f, "slow": 60}), session_id=sid)
    _ledger(paths, {"sess-a": ("BTC-USD", "trend"), "sess-b": ("BTC-USD", "volatility_filter")})
    tag_trials(paths, store)
    clusters.record(store, clusters.compute(paths, store, dataset[1].split_id))


def test_archives_are_byte_identical_on_regeneration_and_reruns_are_not_trials(paths, store, dataset):
    from quant_research.memory.archive import write_archives

    _memory_home(paths, store, dataset)
    split = dataset[1].split_id
    trials = store.trial_count(split_id=split)
    first = {p.name: p.read_bytes() for p in write_archives(paths, store)}
    assert store.trial_count(split_id=split) == trials
    assert {p.name: p.read_bytes() for p in write_archives(paths, store)} == first
    assert {"tsmom.json", "tsmom.md", "short_term_reversal.json", "vol_overlay.md"} <= set(first)


def test_archive_lists_members_representative_costs_and_regimes(paths, store, dataset):
    from quant_research.memory.archive import build_archives

    _memory_home(paths, store, dataset)
    v = build_archives(paths, store)["tsmom"]["splits"][dataset[1].split_id]
    assert v["product"] == "BTC-USD"
    assert {m["instance"] for m in v["members"]} == {"btc-tsmom-1w", "btc-tsmom-1m"}
    assert v["representative"]["experiment_id"] in {m["experiment_id"] for m in v["members"]}
    assert set(v["cost"]) >= {"sharpe_1x", "sharpe_2x"} and v["cost"]["sharpe_2x"] is not None
    assert set(v["regimes"]) == {"trend_up", "trend_down", "high_vol", "low_vol"}
    assert set(v["correlation"]) == {"channel_breakout"}
    assert v["k"]["0.7"] >= 1 and v["members"][0]["split_dsr"] is not None
