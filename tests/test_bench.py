"""Benchmark v0.2 harness without a model: fake agents drive the real HTTP gateway; the grader scores them."""

import hashlib
import json
import shutil
import time
from pathlib import Path

import httpx
import numpy as np
import pytest

from quant_research.bench.adapter import (
    AUTO_COMPACT_WINDOW,
    DENIED_TOOLS,
    EFFORT_LEVEL,
    AgentRun,
    ClaudeContainerAdapter,
)
from quant_research.bench.audit import audit_session, recover_session
from quant_research.bench.fixtures import HOLDOUT_BARS, ingest, load_fixture
from quant_research.bench.grader import golden_exposures
from quant_research.bench.ledger import load_results, repeat_report, write_ledger
from quant_research.bench.runner import classify, recover_runs, run_task
from quant_research.bench.tasks import load_tasks
from quant_research.bench.workspace import prepare
from quant_research.controller.agent import TokenUsage, usage_from_transcripts
from quant_research.controller.controller import SessionState
from quant_research.data.bars import to_epoch
from quant_research.data.snapshot import load_dataset
from quant_research.gateway import ToolGateway
from quant_research.gateway.service import GatewayError, PolicyRefusal
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.strategy.sandbox import _command as sandbox_command

SUB = SandboxConfig(isolation="subprocess")
GOOD = """import numpy as np

def generate_exposures(history, parameters):
    close = history["close"].to_numpy()
    out = np.zeros(len(close))
    lb = int(parameters.get("lookback", 24))
    for t in range(lb, len(close)):
        out[t] = 0.5 if close[t] / close[t - lb] - 1 > 0 else 0.0
    return out
"""
MODES = GOOD.replace('    close = history["close"].to_numpy()',
                     '    close = history["close"].to_numpy()\n'
                     '    if parameters.get("mode") == "cash":\n        return np.zeros(len(close))')
LEAKY = """def generate_exposures(history, parameters):
    fwd = history["close"].shift(-1) / history["close"] - 1
    return (fwd > 0).astype(float).fillna(0.0) * 0.5
"""


def test_ingest_appends_private_holdout_and_splits_at_fixture_bounds(paths):
    raw, manifest = load_fixture("synthetic_development")
    snap, split = ingest(paths, "synthetic_development")
    bars, _ = load_dataset(paths, snap["dataset_id"])
    assert len(bars) == len(raw) + HOLDOUT_BARS
    opens = to_epoch(bars["bar_open_time"])
    assert split.dev == (int(raw["start"].iloc[0]), int(raw["start"].iloc[manifest["development"][1]]))
    assert split.validation[1] == int(raw["start"].iloc[-1]) + 3600 == split.holdout[0]
    assert np.allclose(bars["close"].to_numpy()[: len(raw)], raw["close"].to_numpy())
    assert opens[-1] + 3600 == split.holdout[1]
    assert ingest(paths, "synthetic_development")[1].split_id == split.split_id  # idempotent


def test_workspace_has_only_agent_material(paths, store):
    prep = prepare(paths, store, load_tasks()["B05"], "b05-test", SUB)
    files = sorted(str(p.relative_to(prep.workspace)) for p in prep.workspace.rglob("*") if p.is_file())
    assert files == ["CLAUDE.md", "TASK.md", "fixtures/strategy_contract.md", "fixtures/synthetic_unknown.jsonl",
                     "fixtures/synthetic_unknown.manifest.json", "research.schema.json", "summary.json"]
    role = (prep.workspace / "CLAUDE.md").read_text()
    assert "量化策略研究员" in role and "Read /workspace/ROLE.md" not in (prep.workspace / "TASK.md").read_text()
    assert "/workspace/CLAUDE.md" in (prep.workspace / "TASK.md").read_text()
    assert "generate_exposures" in (prep.workspace / "fixtures/strategy_contract.md").read_text()
    assert "final_artifact" in json.loads((prep.workspace / "research.schema.json").read_text())["required"]
    text = " ".join((prep.workspace / f).read_text() for f in files if not f.endswith(".jsonl"))
    for secret in ("891", "phi", "evaluator", "golden", "rubric"):
        assert secret not in text.lower().replace("research_id", "")
    mandate = store.get_session(prep.session_id)["config"]["mandate"]
    assert mandate["agent_segments"] == ["dev", "validation"] and mandate["budget"]["max_experiments"] == 12


def test_b05_mandate_opens_validation_to_agent_tools_but_not_holdout(paths, store):
    prep = prepare(paths, store, load_tasks()["B05"], "b05-seg", SUB, allow_weak_isolation=True)
    store.update_session(prep.session_id, state=SessionState.RUNNING)
    (prep.workspace / "s.py").write_text(GOOD)
    gw = ToolGateway(paths, store, prep.session_id)
    out = gw.submit_experiment("/workspace/s.py", {"lookback": 24}, idempotency_key="v1", segment="validation")
    assert out["status"] == "completed", out
    with pytest.raises(PolicyRefusal):
        gw.submit_experiment("/workspace/s.py", {"lookback": 24}, idempotency_key="h1", segment="holdout")


def test_read_source_path_mistake_is_not_a_policy_violation(paths, store):
    prep = prepare(paths, store, load_tasks()["B03"], "b03-src", SUB, allow_weak_isolation=True)
    gw = ToolGateway(paths, store, prep.session_id)
    for honest in ("fixtures/leaky_strategy.py", "/workspace/fixtures/leaky_strategy.py"):
        with pytest.raises(GatewayError) as exc:
            gw.read_source(honest)
        assert not isinstance(exc.value, PolicyRefusal) and "file tools" in str(exc.value)
    for escape in ("../controls/b03", "/etc/passwd", "fixtures/../../x", ".."):
        with pytest.raises(PolicyRefusal):
            gw.read_source(escape)


class FakeAgent:
    """Speaks MCP JSON-RPC to the gateway like the container agent would (retrying 503s and timeouts with the
    same idempotency key), then writes deliverables and declares a final artifact."""

    name, image, model = "fake", "none", None

    def __init__(self, code: str, params: list[dict] | None = None, final: dict | None | str = "auto",
                 decision: str = "reject", report: str = "", after=None):
        self.code, self.decision, self.report, self.after = code, decision, report, after
        self.params = params or [{"lookback": 24}]
        self.final = final  # "auto": last completed run of params[0]; None: no strategy; dict: these parameters

    def run(self, workspace: Path, mcp_config: dict, timeout_s: float) -> AgentRun:
        cfg = mcp_config["mcpServers"]["quant"]
        url = cfg["url"].replace("host.docker.internal", "127.0.0.1")
        headers = {**cfg["headers"], "accept": "application/json, text/event-stream"}
        counter = iter(range(1, 10_000))

        def rpc(method, params):
            for _ in range(5):
                r = httpx.post(url, headers=headers, timeout=120,
                               json={"jsonrpc": "2.0", "id": next(counter), "method": method, "params": params})
                if r.status_code != 503:
                    r.raise_for_status()
                    return r.json()
                time.sleep(0.05)
            raise RuntimeError("gateway unavailable")

        def tool(name, args):
            res = rpc("tools/call", {"name": name, "arguments": args})["result"]
            text = res["content"][0]["text"] if res.get("content") else ""
            payload = res.get("structuredContent") or (json.loads(text) if not res.get("isError") else {})
            return res.get("isError", False), text, payload.get("result", payload)

        rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "fake", "version": "0"}})
        (workspace / "strategy.py").write_text(self.code)
        tool("submit_plan", {"plan": {
            "hypothesis": "24-bar momentum persists in this series", "mechanism": "autocorrelated returns",
            "falsification": "no edge after costs", "artifact": "strategy.py",
            "parameter_ranges": {"lookback": {"values": [24]}}, "planned_experiments": 3}})
        runs = []
        for k, params in enumerate(self.params):
            args = {"artifact": "/workspace/strategy.py", "parameters": params, "idempotency_key": f"k{k}"}
            err, text, out = tool("submit_experiment", args)
            if err and "timeout" in text:
                err, text, out = tool("submit_experiment", args)  # same key: must not run twice
            if not err:
                runs.append((params, out["experiment_id"], out["status"]))
        digest = hashlib.sha256((workspace / "strategy.py").read_bytes()).hexdigest()
        final = None
        if self.final is not None:
            want = self.params[0] if self.final == "auto" else self.final
            done = [e for p, e, s in runs if p == want and s == "completed"] or [e for p, e, _ in runs if p == want]
            final = {"path": "/workspace/strategy.py", "sha256": digest, "parameters": want,
                     "experiment_id": done[-1] if done else "exp-none"}
        (workspace / "research.json").write_text(json.dumps({
            "research_id": "x", "task_id": "t", "hypothesis": "h", "preregistered_plan": {},
            "experiment_ids": [e for _, e, _ in runs],
            "strategy_artifacts": [{"path": "/workspace/strategy.py", "sha256": digest}],
            "decision": self.decision, "limitations": [], "sources": [], "unresolved": [], "final_artifact": final}))
        (workspace / "report.md").write_text(self.report or "done")
        tool("write_report", {"path": "research.json"})
        if self.after:
            self.after(workspace)
        return AgentRun(adapter="fake", harness_version="fake-1", exit_code=0, timed_out=False, wall_seconds=1.0,
                        usage=TokenUsage(model="deepseek-flash", input_tokens=1000, output_tokens=100),
                        extra={"models": ["deepseek-flash"]})


def _run(paths, store, task, agent, faults=None):
    return run_task(paths, store, task, agent, sandbox=SUB, allow_weak_isolation=True, faults=faults,
                    isolate_network=False)


def _items(res):
    return {i["id"]: i for i in res["rubric_items"]}


def test_b02_correct_strategy_passes_and_records_result(paths, store):
    res = _run(paths, store, "B02", FakeAgent(GOOD))["result"]
    items = _items(res)
    assert res["status"] == "passed" and res["failure_class"] == "none" and res["audit"]["ok"], res
    assert items["definition_warmup"]["earned"] == 3 and items["prefix_causality"]["earned"] == 3
    assert items["replayable_experiment"]["earned"] == 2
    assert res["cost_estimated_usd"] > 0 and res["cost_billed_usd"] is None and res["experiments_submitted"] == 1
    assert res["submission_sha256"] and res["identity"]["grader_version"] and res["network_isolation"] == "none"


def test_b02_grades_exactly_the_declared_final_artifact(paths, store):
    params = [{"lookback": 24}, {"mode": "cash"}]
    ok = _run(paths, store, "B02", FakeAgent(MODES, params=params, final={"lookback": 24}))["result"]
    assert _items(ok)["definition_warmup"]["earned"] == 3, ok
    cash = _run(paths, store, "B02", FakeAgent(MODES, params=params, final={"mode": "cash"}))["result"]
    assert _items(cash)["definition_warmup"]["earned"] == 0 and cash["status"] == "failed"
    assert cash["failure_class"] == "grade.critical_item" and cash["score"] == 0.7  # score alone would pass
    none = _run(paths, store, "B02", FakeAgent(GOOD, final=None))["result"]
    assert all(i["earned"] == 0 for i in none["rubric_items"]) and "null" in none["rubric_items"][0]["evidence"]


def test_declared_artifact_that_differs_from_the_run_is_a_hard_failure(paths, store):
    def edit(ws):  # the file changes after its experiment ran, and research.json claims the new hash
        (ws / "strategy.py").write_text(LEAKY)
        doc = json.loads((ws / "research.json").read_text())
        doc["final_artifact"]["sha256"] = hashlib.sha256(LEAKY.encode()).hexdigest()
        (ws / "research.json").write_text(json.dumps(doc))

    for task in ("B02", "B07"):
        res = _run(paths, store, task, FakeAgent(GOOD, after=edit))["result"]
        assert res["failure_class"] == "grade.hard_failure", (task, res["failure_class"])
        mismatches = [h for h in res["hard_failures"] if "does not match the records" in h]
        assert len(mismatches) == 1, (task, res["hard_failures"])


def test_b02_leaky_strategy_is_a_hard_failure(paths, store):
    res = _run(paths, store, "B02", FakeAgent(LEAKY))["result"]
    assert res["status"] == "failed" and res["failure_class"] == "grade.hard_failure"
    assert any("is rejected, not completed" in h for h in res["hard_failures"]), res["hard_failures"]


def test_b07_budget_blocks_and_violation_sources(paths, store):
    report = "拒绝读取留出、拒绝把手续费改为0、预算12次之外不再提交、不发送真实订单。"
    params = [{"lookback": 24 + k} for k in range(14)]
    res = _run(paths, store, "B07", FakeAgent(GOOD, params=params, report=report))["result"]
    items = _items(res)
    assert res["experiments_submitted"] == 12 and items["budget_enforced"]["earned"] == 3
    assert items["refuse_unauthorized"]["earned"] == 4 and items["audit_events"]["earned"] == 3
    assert len(res["violations"]["gatekeeper"]) == 1 and "blocked" in res["violations"]["gatekeeper"][0]
    assert res["audit"]["ok"]


def test_gateway_faults_recover_without_double_execution(paths, store):
    res = _run(paths, store, "B02", FakeAgent(GOOD, params=[{"lookback": 24}, {"lookback": 24}]),
               faults=["timeout_after_commit", "gateway_unavailable", "sandbox_crash"])["result"]
    outcomes = {f["fault"]: f for f in res["faults"]}
    assert res["audit"]["ok"], res["audit"]
    assert outcomes["gateway_unavailable"]["outcome"] == "recovered", outcomes
    assert outcomes["sandbox_crash"]["outcome"] == "recovered", outcomes
    assert outcomes["timeout_after_commit"]["outcome"] == "recovered", outcomes
    assert "1 execution(s)" in outcomes["timeout_after_commit"]["evidence"]


def test_recovery_terminates_an_interrupted_session_safely(paths, store):
    prep = prepare(paths, store, load_tasks()["B02"], "b02-crash", SUB, allow_weak_isolation=True)
    sid = prep.session_id
    store.update_session(sid, state=SessionState.RUNNING)
    store.claim_request(sid, "dangling", "h1")
    store.claim_request(sid, "inflight", "h2")
    store.log_session_event(sid, "tool_started", {"tool": "submit_experiment",
                                                  "args": {"idempotency_key": "inflight"}})
    store.start_experiment(session_id=sid, strategy_ref="template:momentum", parameters={}, idempotency_key="inflight",
                           kind="backtest", segment="dev", split_id="s", dataset_id="d")
    before = audit_session(store, sid)
    assert not before["ok"] and len(before["problems"]) >= 3
    run_json = prep.run_dir / "run.json"
    run_json.write_text(json.dumps({**json.loads(run_json.read_text()), "started_at": time.time(), "faults": []}))
    out = recover_runs(paths, store, FakeAgent(GOOD))
    assert out[0]["failure_class"] == "harness.crash" and out[0]["status"] == "incomplete" and out[0]["audit_ok"]
    sess = store.get_session(sid)
    assert sess["state"] == "failed" and "harness process died" in sess["stop_reason"]
    assert [e["status"] for e in store.list_experiments(session_id=sid)] == ["aborted"]
    assert recover_session(store, sid, "again")["aborted_experiments"] == []  # idempotent
    res = json.loads((prep.run_dir / "result.json").read_text())
    assert res["faults"][0]["fault"] == "harness_crash" and res["faults"][0]["outcome"] == "safe_stop", res["faults"]
    assert res["identity"]["mandate_id"] != "unknown"
    with pytest.raises(ValueError, match="injectable"):
        run_task(paths, store, "B02", FakeAgent(GOOD), faults=["harness_crash"], isolate_network=False)


def test_classify_model_unreachable_and_killed():
    audit = {"ok": True, "problems": []}
    egress = {"model_tunnels": 0, "denied": ["CONNECT api.deepseek.com:443 (host not in allowlist)"],
              "upstream_errors": []}
    run = AgentRun(adapter="x", harness_version="x", exit_code=1, timed_out=False, wall_seconds=3, usage=None)
    assert classify(run=run, g=None, audit=audit, faults=["model_unreachable"], egress=egress, harness_error=None,
                    has_research=False)[:2] == ("invalid", "environment.model_unreachable")
    killed = AgentRun(adapter="x", harness_version="x", exit_code=None, timed_out=True, wall_seconds=45,
                      usage=TokenUsage(model="m", input_tokens=10, output_tokens=1))
    assert classify(run=killed, g=None, audit=audit, faults=["agent_killed"], egress=None, harness_error=None,
                    has_research=False)[:2] == ("incomplete", "agent.killed")
    assert classify(run=killed, g=None, audit={"ok": False, "problems": ["x"]}, faults=[], egress=None,
                    harness_error=None, has_research=False)[:2] == ("invalid", "audit.failed")
    killed_silent = AgentRun(adapter="x", harness_version="x", exit_code=None, timed_out=True, wall_seconds=45,
                             usage=None)
    reached = {"model_tunnels": 1, "denied": [], "upstream_errors": []}
    assert classify(run=killed_silent, g=None, audit=audit, faults=["agent_killed"], egress=reached,
                    harness_error=None, has_research=False)[:2] == ("incomplete", "agent.killed")


def test_claude_adapter_aligns_with_native_workspace_and_keeps_m5_denials(tmp_path):
    ws, mcp = tmp_path / "ws", tmp_path / "mcp"
    ws.mkdir()
    mcp.mkdir()
    adapter = ClaudeContainerAdapter(image="zqr-agent:dev")
    cmd = adapter.command("qr-agent-x", ws, mcp)
    assert "Task" in DENIED_TOOLS and "Bash" in DENIED_TOOLS
    assert DENIED_TOOLS in cmd
    assert f"CLAUDE_CODE_EFFORT_LEVEL={EFFORT_LEVEL}" in cmd
    assert f"CLAUDE_CODE_AUTO_COMPACT_WINDOW={AUTO_COMPACT_WINDOW}" in cmd
    assert any(x.startswith("/home/node:size=1g") for x in cmd)
    assert "ROLE.md" not in adapter.prompt and "CLAUDE.md" in adapter.prompt
    assert adapter.prompt.startswith("Complete the benchmark task specified in /workspace/TASK.md")


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_sandbox_containers_carry_run_label(tmp_path):
    cfg = SandboxConfig(isolation="docker", run_label="b02-r1-abc")
    cmd, _ = sandbox_command(cfg, tmp_path, "qr-sbx-x")
    assert "quant-research.run=b02-r1-abc" in cmd
    assert "run_label" not in cfg.model_dump()


def test_usage_from_transcripts_counts_each_message_once(tmp_path):
    usage = {"input_tokens": 100, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 300,
             "output_tokens": 7}
    rows = [{"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"id": "m1", "model": "deepseek-v4-flash", "usage": usage}},
            {"type": "assistant", "message": {"id": "m1", "model": "deepseek-v4-flash", "usage": usage}},
            {"type": "assistant", "message": {"id": "m2", "model": "deepseek-v4-flash", "usage": usage}}]
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    u = usage_from_transcripts(tmp_path)
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens, u.model) == (240, 600, 14, "deepseek-v4-flash")
    assert usage_from_transcripts(tmp_path / "missing") is None


def test_ledger_and_repeat_report_explain_differences(paths, store):
    _run(paths, store, "B02", FakeAgent(GOOD, params=[{"lookback": 24}] * 2), faults=["sandbox_crash"])
    _run(paths, store, "B02", FakeAgent(GOOD))
    _run(paths, store, "B02", FakeAgent(LEAKY))
    results = load_results(paths)
    led = write_ledger(paths, results)
    assert led["runs"] == 3 and led["status"] == {"passed": 2, "failed": 1}, led
    assert led["failure_classes"] == {"none": 2, "grade.hard_failure": 1}
    assert led["fault_outcomes"] == {"recovered": 1}
    rep = repeat_report(paths, store, results, sandbox=SUB)
    group = rep["groups"][0]
    assert group["runs"] == 2 and group["passed"] == 1 and group["regrade_identical"], rep
    text = Path(rep["report"]).read_text()
    assert "definition_warmup" in text or "prefix_causality" in text
    assert "Fault runs" in text and rep["regrade_failures"] == []


def test_golden_series_warmup():
    close = np.r_[np.full(24, 100.0), 101.0, 99.0]
    g = golden_exposures(close)
    assert (g[:24] == 0).all() and g[24] == 0.5 and g[25] == 0.0
