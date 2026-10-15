"""Strategy inbox: store and commands, the drafting run in the research loop, campaigns written from drafts, budget,
recovery and the report."""

import json

import pytest

from quant_research.bench.adapter import AgentRun
from quant_research.campaign import budget
from quant_research.campaign.store import CampaignStore
from quant_research.controller.agent import TokenUsage
from quant_research.desk import research as rl
from quant_research.inbox.store import InboxError, InboxStore
from quant_research.timeutil import to_iso
from quant_research.validation.splits import load_split
from tests.test_campaign import FakeAgent, _loop, _queued
from tests.test_hypothesis import _reference

TEXT = ("Weekend breakout: buy BTC when Monday's close breaks the weekend high, hold three days. "
        "Backtested 2019-2025 with a 70% win rate. Published 2025-11-03.")


def _proposal(**kw):
    return {"product": "BTC-USD", "family": "channel_breakout",
            "goal": "Test whether a Monday close above the weekend high predicts a three-day continuation.",
            "prior": "The author claims a 70% win rate; testable as a breakout of the 48-bar weekend high.",
            "overlap": ""} | kw


class FakeDrafter:
    """Writes the scripted draft into the workspace. Actions: a dict (the draft), "bad" (invalid JSON), "none" (no
    file), "env_fail", "die"."""

    name = "fake-drafter"

    def __init__(self, script, tokens=20_000, model="deepseek-v4-flash"):
        self.script, self.tokens, self.model = list(script), tokens, model
        self.calls, self.workspaces = 0, []

    def run(self, workspace, mcp_config, timeout_s, network=None, label=None, transcript_dir=None):
        assert mcp_config == rl.NO_TOOLS
        self.calls += 1
        self.workspaces.append({p.name: p.read_text() for p in workspace.iterdir()})
        action = self.script[min(self.calls, len(self.script)) - 1]
        if action == "env_fail":
            return AgentRun(adapter="fake", harness_version="fake", exit_code=1, timed_out=False, wall_seconds=0.1,
                            usage=None, stderr_tail="Cannot connect to the Docker daemon")
        if action == "die":
            raise SystemExit("desk process killed")
        if action == "bad":
            (workspace / "draft.json").write_text("{not json")
        elif isinstance(action, dict):
            (workspace / "draft.json").write_text(json.dumps(action))
        return AgentRun(adapter="fake", harness_version="fake", exit_code=0, timed_out=False, wall_seconds=1.0,
                        usage=TokenUsage(model=self.model, input_tokens=self.tokens, output_tokens=2_000),
                        extra={"models": [self.model]})


def _inbox_loop(paths, store, script, agent=None, **cfg_kw):
    loop, clock = _loop(paths, store, agent or FakeAgent(paths, store), **cfg_kw)
    loop.drafter = FakeDrafter(script)
    return loop, clock


def _add(paths, store, text=TEXT, **kw):
    return InboxStore(store).add(paths, text, **kw)["id"]


# -- store -----------------------------------------------------------------------------------------------------

def test_add_validates_and_refuses_duplicates(paths, store, dataset):
    _reference(paths, store, dataset)
    inbox = InboxStore(store)
    row = inbox.add(paths, f"  {TEXT}\n", url="https://x.com/a/1", published="2025-11-03", note="seen today")
    assert row["id"].startswith("inb-") and row["state"] == "received" and row["text"] == TEXT
    with pytest.raises(InboxError, match=f"already in the inbox as {row['id']}"):
        inbox.add(paths, TEXT.replace(" ", "   "))
    for kw, msg in (({"text": "   "}, "empty"), ({"text": "x" * 20_001}, "at most 20000"),
                    ({"text": "a", "published": "3 Nov"}, "YYYY-MM-DD"),
                    ({"text": "b", "product": "SOL-USD"}, "no desk mandate")):
        with pytest.raises(InboxError, match=msg):
            inbox.add(paths, **kw)


def test_retry_and_discard_are_the_humans_and_follow_the_states(paths, store, dataset):
    _reference(paths, store, dataset)
    inbox = InboxStore(store)
    iid = _add(paths, store)
    with pytest.raises(InboxError, match="cannot become received"):
        inbox.transition(iid, "received", "human", "retry")
    with pytest.raises(InboxError, match="for the controller"):
        inbox.transition(iid, "drafting", "human", "x")
    with pytest.raises(InboxError, match="needs a note"):
        inbox.transition(iid, "discarded", "human", "")
    assert inbox.transition(iid, "discarded", "human", "not interesting")["state"] == "discarded"
    with pytest.raises(InboxError, match="cannot become drafting"):
        inbox.transition(iid, "drafting", "controller")


# -- drafting run --------------------------------------------------------------------------------------------

def test_a_draft_becomes_proposed_campaigns_with_provenance(paths, store, dataset):
    _reference(paths, store, dataset)
    iid = _add(paths, store, url="https://x.com/a/1", note="from a thread")
    loop, clock = _inbox_loop(paths, store, [{"verdict": "proposals", "published": "2025-11-03",
                                              "proposals": [_proposal(overlap="no earlier work on breakouts"),
                                                            _proposal(product="ETH-USD")]}])
    out = loop.step()
    res = out["result"]
    assert out["ran"] and res["topic"] == "inbox" and res["failure_class"] == "none"
    row = InboxStore(store).require(iid)
    assert row["state"] == "drafted" and len(row["campaign_ids"]) == 1 and row["cost_usd"] > 0
    assert row["draft"]["refused"] == [{"product": "ETH-USD", "family": "channel_breakout",
                                        "reason": "ETH-USD has no desk mandate"}]
    camp = CampaignStore(store).require(row["campaign_ids"][0])
    assert camp["state"] == "proposed" and camp["family"] == "channel_breakout"
    assert camp["source"] == {"kind": "inbox", "inbox_id": iid, "published": "2025-11-03",
                              "may_have_seen_validation": True}
    assert camp["body"]["citations"] == [{"kind": "published", "ref": "https://x.com/a/1"}]
    assert camp["body"]["prior"].endswith("Earlier work: no earlier work on breakouts")
    ws = loop.drafter.workspaces[0]
    assert set(ws) == {"TASK.md", "CONSTRAINTS.md", "LIBRARY.md", "draft.schema.json"}
    assert TEXT in ws["TASK.md"] and "untrusted external material" in ws["TASK.md"]
    assert "from a thread" in ws["TASK.md"] and "0.60%" in ws["CONSTRAINTS.md"]
    assert "`channel_breakout`" in ws["LIBRARY.md"]
    ledger = rl.read_ledger(paths)[-1]
    assert ledger["topic"] == "inbox" and ledger["inbox_id"] == iid and ledger["campaigns_written"] == row[
        "campaign_ids"] and ledger["day"] == rl.utc_day(clock.t)
    assert rl.load_state(paths)["sessions_started"] == 0


def test_infeasible_invalid_and_refused_drafts(paths, store, dataset):
    _reference(paths, store, dataset)
    inbox = InboxStore(store)
    a, b, c = (_add(paths, store, t) for t in ("short ETH funding", "grid bot", "a family nobody has"))
    d = _add(paths, store, "breakout on ETH", product="BTC-USD")
    loop, clock = _inbox_loop(paths, store, [
        {"verdict": "infeasible", "reason": "needs funding rates and shorting"},
        "bad",
        {"verdict": "proposals", "proposals": [_proposal(family="astrology")]},
        {"verdict": "proposals", "proposals": [_proposal(product="ETH-USD"), _proposal()]},
    ])
    for _ in range(4):
        assert loop.step()["ran"]
        clock.t += 120  # past the backoff after the invalid draft
    assert inbox.require(a)["state"] == "infeasible" and "funding" in inbox.require(a)["reason"]
    assert inbox.require(b)["state"] == "failed" and "agent.no_deliverable" in inbox.require(b)["reason"]
    row = inbox.require(c)
    assert row["state"] == "drafted" and row["campaign_ids"] == [] and "not a sleeve family" in row["reason"]
    row = inbox.require(d)
    assert len(row["campaign_ids"]) == 1 and "a second proposal" in row["reason"]
    assert CampaignStore(store).require(row["campaign_ids"][0])["product"] == "BTC-USD"
    assert inbox.transition(b, "received", "human", "retry")["state"] == "received"


def test_publication_warning(paths, store, dataset):
    from quant_research.inbox.draft import may_have_seen_validation

    _reference(paths, store, dataset)
    start = load_split(paths, dataset[1].split_id).validation[0]
    before, after = to_iso(start - 86_400 * 30)[:10], to_iso(start + 86_400 * 30)[:10]
    assert may_have_seen_validation(paths, store, "BTC-USD", after)
    assert not may_have_seen_validation(paths, store, "BTC-USD", before)
    assert may_have_seen_validation(paths, store, "BTC-USD", None)
    iid = _add(paths, store, published=before)
    loop, _ = _inbox_loop(paths, store, [{"verdict": "proposals", "published": after, "proposals": [_proposal()]}])
    loop.step()
    camp = CampaignStore(store).require(InboxStore(store).require(iid)["campaign_ids"][0])
    assert camp["source"]["published"] == before and camp["source"]["may_have_seen_validation"] is False


def test_drafting_comes_before_the_next_campaign_session(paths, store, dataset):
    _reference(paths, store, dataset)
    _queued(paths, store)
    agent = FakeAgent(paths, store)
    loop, _ = _inbox_loop(paths, store, [{"verdict": "infeasible", "reason": "shorting"}], agent=agent)
    iid = _add(paths, store)
    assert loop.step()["result"]["topic"] == "inbox" and agent.calls == 0
    assert loop.step()["result"]["topic"] == "campaign" and agent.calls == 1
    assert InboxStore(store).require(iid)["state"] == "infeasible"
    assert TEXT not in agent.briefs[0]


def test_the_daily_limit_and_budget_hold_items_back(paths, store, dataset):
    _reference(paths, store, dataset)
    loop, clock = _inbox_loop(paths, store, [{"verdict": "infeasible", "reason": "shorting"}],
                              inbox_drafts_per_day=1)
    a, b = _add(paths, store, "one"), _add(paths, store, "two")
    assert loop.step()["ran"]
    out = loop.step()
    assert out["status"] == "idle" and loop.drafter.calls == 1
    assert InboxStore(store).require(b)["state"] == "received"
    clock.t += 86_400
    day = rl.utc_day(clock.t)
    with open(rl.ledger_path(paths), "a") as f:
        f.write(json.dumps({"day": day, "cost_usd": 4.95}) + "\n")
    assert not loop.step()["ran"] and loop.drafter.calls == 1
    rl.ledger_path(paths).write_text("")
    assert loop.step()["ran"] and InboxStore(store).require(b)["state"] == "infeasible"
    assert InboxStore(store).require(a)["state"] == "infeasible"


def test_inbox_runs_do_not_use_the_session_count(paths, store, dataset):
    _reference(paths, store, dataset)
    _queued(paths, store)
    loop, clock = _loop(paths, store, FakeAgent(paths, store))
    day = rl.utc_day(clock.t)
    rl.ledger_path(paths).write_text("".join(json.dumps({"day": day, "cost_usd": 0.0, "topic": "inbox"}) + "\n"
                                             for _ in range(12)))
    assert loop.step()["ran"]


def test_an_overrun_draft_stops_the_loop(paths, store, dataset):
    _reference(paths, store, dataset)
    _add(paths, store)
    loop, _ = _inbox_loop(paths, store, [{"verdict": "infeasible", "reason": "shorting"}])
    loop.drafter.tokens = 1_000_000  # about 0.29 USD against a 0.10 reservation
    loop.step()
    assert "above its reservation" in budget.active_stop(paths)["reason"]
    assert loop.step()["status"] == "budget_stop"


def test_environment_failures_and_crashes_put_the_item_back(paths, store, dataset):
    _reference(paths, store, dataset)
    iid = _add(paths, store)
    loop, clock = _inbox_loop(paths, store, ["env_fail", "die", {"verdict": "infeasible", "reason": "shorting"}])
    out = loop.step()
    assert out["status"] == "paused" and out["result"]["failure_class"] == "environment.docker"
    assert InboxStore(store).require(iid)["state"] == "received"
    clock.t += 10_000
    with pytest.raises(SystemExit):
        loop.step()
    run_id = rl.load_state(paths)["current_run"]
    assert InboxStore(store).require(iid)["state"] == "drafting"
    out = rl.recover_desk_runs(paths, store, loop.cfg)
    assert [r["run_id"] for r in out] == [run_id] and out[0]["failure_class"] == "harness.crash"
    assert InboxStore(store).require(iid)["state"] == "received"
    assert loop.step()["result"]["inbox_state"] == "infeasible"


def test_without_a_drafter_items_wait(paths, store, dataset):
    _reference(paths, store, dataset)
    _add(paths, store)
    loop, _ = _loop(paths, store, FakeAgent(paths, store))
    assert loop.step()["status"] == "idle"


# -- report and CLI ------------------------------------------------------------------------------------------

def test_report_shows_the_inbox_line(paths, store, dataset):
    from quant_research.desk.report import research_section

    _reference(paths, store, dataset)
    _add(paths, store, published="2020-01-01")
    _add(paths, store, "still waiting")
    loop, clock = _inbox_loop(paths, store, [{"verdict": "proposals", "proposals": [_proposal()]}],
                              inbox_drafts_per_day=1)
    loop.step()
    lines, summary = research_section(paths, store, rl.utc_day(clock.t))
    text = "\n".join(lines)
    assert summary["inbox_drafts"] == 1 and summary["sessions"] == 0
    assert "drafting runs (drafted 1)" in text and "1 waiting" in text
    assert "from inb-" in text and "published 2020-01-01" in text and "may have seen" not in text


def test_inbox_cli(paths, store, dataset, capsys, tmp_path, monkeypatch):
    import io

    from quant_research.cli import main

    _reference(paths, store, dataset)
    home = str(paths.root)
    monkeypatch.setattr("sys.stdin", io.StringIO(TEXT))
    main(["--home", home, "inbox", "add", "--url", "https://x.com/a/1", "--product", "BTC-USD"])
    iid = json.loads(capsys.readouterr().out)["id"]
    f = tmp_path / "post.txt"
    f.write_text("another idea")
    main(["--home", home, "inbox", "add", "--file", str(f)])
    other = json.loads(capsys.readouterr().out)["id"]
    main(["--home", home, "inbox", "list", "--state", "received"])
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == [iid, other]
    main(["--home", home, "inbox", "show", iid])
    shown = json.loads(capsys.readouterr().out)
    assert shown["text"] == TEXT and shown["events"][0]["event"] == "received" and shown["campaigns"] == []
    main(["--home", home, "inbox", "discard", other, "--note", "duplicate idea"])
    assert json.loads(capsys.readouterr().out)["state"] == "discarded"
    with pytest.raises(SystemExit, match="cannot become received"):
        main(["--home", home, "inbox", "retry", other])
    with pytest.raises(SystemExit, match="already in the inbox"):
        main(["--home", home, "inbox", "add", "--file", str(f)])
