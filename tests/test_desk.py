"""Research desk: standing authorization and automatic promotion (the research loop: tests/test_campaign.py)."""

import json
import time

import pytest

from quant_research.controller.agent import AgentRoundResult, CandidateSubmission, ScriptedRunner
from quant_research.controller.controller import ResearchController, SessionConfig
from quant_research.desk import promotion
from quant_research.desk.config import DeskConfig
from quant_research.paper.fleet import load_roster, save_roster
from quant_research.research import authorization as auth_mod
from quant_research.research.authorization import AuthorizationError
from quant_research.research.registry import RegistryError, freeze, transition
from quant_research.strategy.sandbox import SandboxConfig
from tests.conftest import make_mandate

EASY_HOLDOUT = {"max_viewings": 3, "min_primary": -100.0, "min_total_return": -1.0}


def _validated(paths, store, dataset, template="buy_and_hold", params=None, **mandate_kw):
    agent = ScriptedRunner(lambda t: AgentRoundResult(
        candidates=[CandidateSubmission(template=template, parameters=params or {})], done=True))
    ctl = ResearchController(paths, store, agent)
    cfg = SessionConfig(mandate=make_mandate(dataset, **mandate_kw), sandbox=SandboxConfig(isolation="subprocess"))
    summary = ctl.run(ctl.create_session(cfg))
    return summary["candidates"][0]["candidate_id"]


def _authorize(paths, **kw):
    args = {"products": ["BTC-USD"], "max_paper_strategies": 2, "days": 7} | kw
    auth = auth_mod.create(**args)
    auth_mod.save(promotion.authorization_path(paths), auth)
    return auth


def test_authorization_seal_expiry_scope_and_product():
    now = time.time()
    auth = auth_mod.create(["BTC-USD"], 3, days=1, now=now)
    auth.check(now=now + 10, transition=("validated", "frozen"), product="BTC-USD")
    with pytest.raises(AuthorizationError, match="expired"):
        auth.check(now=now + 86401)
    with pytest.raises(AuthorizationError, match="not an authorized product"):
        auth.check(now=now, product="ETH-USD")
    with pytest.raises(AuthorizationError, match="outside the authorization scope"):
        auth.check(now=now, transition=("paper_approved", "paper_active"))
    tampered = auth.model_copy(update={"max_paper_strategies": 99})
    with pytest.raises(AuthorizationError, match="hash"):
        tampered.check(now=now)


def test_desk_actor_needs_a_valid_authorization(paths, store, dataset):
    cid = _validated(paths, store, dataset)
    with pytest.raises(RegistryError, match="standing authorization"):
        freeze(paths, store, cid, "", actor="desk")
    expired = auth_mod.create(["BTC-USD"], 1, days=1, now=time.time() - 2 * 86400)
    with pytest.raises(RegistryError, match="expired"):
        freeze(paths, store, cid, "", actor="desk", authorization=expired)
    with pytest.raises(RegistryError, match="not allowed for controller"):
        freeze(paths, store, cid, "", actor="controller")
    auth = auth_mod.create(["BTC-USD"], 1, days=1)
    cand = freeze(paths, store, cid, "", actor="desk", authorization=auth)
    assert cand["state"] == "frozen" and cand["evidence"]["authorization_hash"] == auth.hash
    event = store.candidate_events(cid)[-1]
    assert event["actor"] == "desk"
    with pytest.raises(RegistryError, match="not allowed for desk"):
        transition(store, cid, "final_evaluated", "desk", authorization=auth)  # only the evaluator decides


def test_promotion_runs_to_the_roster_and_activates(paths, store, dataset):
    cid = _validated(paths, store, dataset, holdout=EASY_HOLDOUT)
    assert promotion.promote(paths, store, cid)["stopped"].startswith("no standing authorization")
    assert store.get_candidate(cid)["state"] == "validated"
    _authorize(paths)
    out = promotion.promote(paths, store, cid)
    assert out["stopped"] == "done", out
    assert out["steps"] == ["frozen", "final_evaluated", "paper_approved", "roster"]
    entry = load_roster(promotion.roster_path(paths)).entries[-1]
    assert entry.candidate_id == cid and entry.source == "candidate" and entry.product == "BTC-USD"
    promotion.activate_hook(paths)(entry)
    assert store.get_candidate(cid)["state"] == "paper_active"


def test_promotion_respects_product_cap_and_holdout_limit(paths, store, dataset):
    _authorize(paths, products=["ETH-USD"])
    cid = _validated(paths, store, dataset, holdout=EASY_HOLDOUT)
    assert "not an authorized product" in promotion.promote(paths, store, cid)["stopped"]

    _authorize(paths, max_paper_strategies=1)
    assert promotion.promote(paths, store, cid)["stopped"] == "done"
    cid2 = _validated(paths, store, dataset, template="momentum", params={"lookback": 24}, holdout=EASY_HOLDOUT)
    out = promotion.promote(paths, store, cid2)
    assert "authorization cap" in out["stopped"] and out["state"] == "validated"  # no holdout viewing spent

    _authorize(paths, max_paper_strategies=3)
    one_view = {**EASY_HOLDOUT, "max_viewings": 1}
    cid3 = _validated(paths, store, dataset, template="momentum", params={"lookback": 48}, holdout=one_view,
                      question="a different question")
    out = promotion.promote(paths, store, cid3)
    assert "exhausted" in out["stopped"] and out["state"] == "validated"  # not frozen on a used-up split
    assert len(store.holdout_runs(dataset[1].split_id)) == 1


def test_promotion_does_not_freeze_on_an_exhausted_split(paths, store, dataset):
    _authorize(paths, max_paper_strategies=3)
    first = _validated(paths, store, dataset, holdout={**EASY_HOLDOUT, "max_viewings": 1})
    assert promotion.promote(paths, store, first)["stopped"] == "done"
    looser = {**EASY_HOLDOUT, "max_viewings": 5}  # a newer mandate cannot reopen the split
    cid = _validated(paths, store, dataset, template="momentum", params={"lookback": 24}, holdout=looser,
                     question="a looser mandate")
    out = promotion.promote(paths, store, cid)
    assert out["stopped"].startswith(f"holdout of {dataset[1].split_id} exhausted") and out["state"] == "validated"
    assert out["steps"] == [] and not store.is_frozen(store.get_candidate(cid)["candidate_hash"])


def test_promotion_does_not_freeze_on_a_closed_split(paths, store, dataset):
    _authorize(paths, max_paper_strategies=3)
    cid = _validated(paths, store, dataset, holdout={**EASY_HOLDOUT, "max_viewings": 5})
    store.close_holdout(dataset[1].split_id, "holdout seen")
    out = promotion.promote(paths, store, cid)
    assert out["stopped"] == f"holdout of {dataset[1].split_id} is closed (holdout seen); not freezing"
    assert out["state"] == "validated" and not store.is_frozen(store.get_candidate(cid)["candidate_hash"])


def test_desk_promote_command_prints_the_result(paths, store, dataset, capsys):
    from quant_research.cli import main

    _authorize(paths)
    cid = _validated(paths, store, dataset, holdout=EASY_HOLDOUT)
    main(["--home", str(paths.root), "desk", "promote", cid, "--isolation", "subprocess"])
    out = json.loads(capsys.readouterr().out)
    assert out["stopped"] == "done" and out["state"] == "paper_approved" and out["candidate_id"] == cid


def test_promotion_resumes_after_a_slot_frees(paths, store, dataset):
    _authorize(paths, max_paper_strategies=1)
    first = _validated(paths, store, dataset, holdout=EASY_HOLDOUT)
    assert promotion.promote(paths, store, first)["stopped"] == "done"
    second = _validated(paths, store, dataset, template="momentum", params={"lookback": 24}, holdout=EASY_HOLDOUT)
    out = promotion.promote(paths, store, second)
    assert "authorization cap" in out["stopped"] and out["state"] == "validated"
    viewings = len(store.holdout_runs(dataset[1].split_id))
    rp = promotion.roster_path(paths)
    roster = load_roster(rp)
    roster.entries[0].enabled = False
    save_roster(rp, roster)
    out = promotion.promote(paths, store, second)
    assert out["stopped"] == "done" and out["state"] == "paper_approved"
    assert len(store.holdout_runs(dataset[1].split_id)) == viewings + 1


def test_promotion_is_idempotent_from_paper_approved(paths, store, dataset):
    _authorize(paths)
    cid = _validated(paths, store, dataset, holdout=EASY_HOLDOUT)
    assert promotion.promote(paths, store, cid)["stopped"] == "done"
    rp = promotion.roster_path(paths)
    roster = load_roster(rp)
    roster.entries = [e for e in roster.entries if e.candidate_id != cid]  # crashed before the roster write
    save_roster(rp, roster)
    viewings = len(store.holdout_runs(dataset[1].split_id))
    out = promotion.promote(paths, store, cid)
    assert out["stopped"] == "done" and out["steps"] == ["roster"]
    assert load_roster(rp).entries[-1].candidate_id == cid
    assert len(store.holdout_runs(dataset[1].split_id)) == viewings


def test_promotion_carries_the_validated_capital_and_costs(paths, store, dataset):
    engine = {"initial_cash": 20_000.0, "costs": {"fee_rate": 0.001, "slippage_bps": 2.0}}
    cid = _validated(paths, store, dataset, holdout=EASY_HOLDOUT, engine_config=engine)
    _authorize(paths)
    assert promotion.promote(paths, store, cid)["stopped"] == "done"
    entry = load_roster(promotion.roster_path(paths)).entries[-1]
    assert (entry.initial_cash, entry.fee_rate, entry.slippage_bps) == (20_000.0, 0.001, 2.0)


def test_promotion_refuses_settings_paper_cannot_reproduce(paths, store, dataset):
    cid = _validated(paths, store, dataset, holdout=EASY_HOLDOUT, engine_config={"execution_delay_bars": 1})
    _authorize(paths)
    out = promotion.promote(paths, store, cid)
    assert "execution_delay_bars=1" in out["stopped"] and out["state"] == "validated"


def test_a_rebalance_tolerance_within_the_band_is_no_paper_gap():
    from quant_research.backtest.engine import EngineConfig
    from quant_research.portfolio.policy import PortfolioPolicy

    band = PortfolioPolicy(band=0.05)
    assert promotion.paper_gaps(EngineConfig(rebalance_tolerance=0.05), band) == []
    assert promotion.paper_gaps(EngineConfig(rebalance_tolerance=0.02), band) == []
    assert promotion.paper_gaps(EngineConfig(rebalance_tolerance=0.1), band) == ["rebalance_tolerance=0.1"]
    assert promotion.paper_gaps(EngineConfig(rebalance_tolerance=0.02), PortfolioPolicy()) == [
        "rebalance_tolerance=0.02"]


def test_use_mandate_switches_the_desk_split_and_records_it(paths, store, dataset, capsys):
    import json

    from quant_research.cli import main
    from quant_research.desk.config import load_config, mandate_history_path, save_config, use_mandate
    from quant_research.desk.report import research_section
    from quant_research.memory.characterise import characterise, seed_runs
    from tests.conftest import make_mandate
    from tests.test_experiments import _extended_split

    manifest, split = dataset
    old = make_mandate(dataset)
    store.put_mandate(old.mandate_id(), old.model_dump(mode="json"))
    save_config(paths, DeskConfig(mandates={"BTC-USD": old.mandate_id()}))
    child_ds, child = _extended_split(paths, manifest, split)
    new = make_mandate((child_ds, child))
    store.put_mandate(new.mandate_id(), new.model_dump(mode="json"))
    with pytest.raises(ValueError, match="research is enabled"):
        use_mandate(paths, store, "BTC-USD", new.mandate_id(), "E1")
    save_config(paths, load_config(paths).model_copy(update={"research_enabled": False}))
    with pytest.raises(ValueError, match="needs a note"):
        use_mandate(paths, store, "BTC-USD", new.mandate_id(), " ")
    with pytest.raises(ValueError, match="not ETH-USD"):
        use_mandate(paths, store, "ETH-USD", new.mandate_id(), "E1")
    main(["--home", str(paths.root), "desk", "use-mandate", "--product", "BTC-USD", "--mandate", new.mandate_id(),
          "--note", "E1: longer dev"])
    out = json.loads(capsys.readouterr().out)
    assert out["changed"] and out["from"] == old.mandate_id() and out["split_id"] == child.split_id
    assert load_config(paths).mandates == {"BTC-USD": new.mandate_id()}
    assert not use_mandate(paths, store, "BTC-USD", new.mandate_id(), "again")["changed"]
    history = [json.loads(x) for x in mandate_history_path(paths).read_text().splitlines()]
    assert [(h["from"], h["to"], h["note"]) for h in history] == [(old.mandate_id(), new.mandate_id(),
                                                                   "E1: longer dev")]
    characterise(paths, store, entries={"tsmom-1w"})
    assert set(seed_runs(store, child.split_id)) == {"btc-tsmom-1w"} and not seed_runs(store, split.split_id)
    store.record_lineage(child.split_id, split.split_id, "E1")
    lines, _ = research_section(paths, store, "2026-10-15")
    row = next(x for x in lines if x.startswith("| BTC-USD | "))
    assert "(1 + 0 inherited)" in row and f"parent `{split.split_id}` viewed 0" in row
