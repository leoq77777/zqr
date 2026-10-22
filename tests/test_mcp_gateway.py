import asyncio
import json

import httpx
from mcp.client import Client

from quant_research.controller.agent import ManualRunner
from quant_research.controller.controller import ResearchController, SessionConfig
from quant_research.gateway import ToolGateway
from quant_research.gateway.mcp_server import GatewayHTTPServer, build_server
from quant_research.strategy.sandbox import SandboxConfig
from tests.conftest import make_mandate


def _session(paths, store, dataset):
    ctl = ResearchController(paths, store, ManualRunner())
    return ctl.create_session(SessionConfig(mandate=make_mandate(dataset),
                                            sandbox=SandboxConfig(isolation="subprocess")))


def _payload(result) -> dict:
    if result.structured_content is not None:
        return result.structured_content.get("result", result.structured_content)
    return json.loads(result.content[0].text)


def test_mcp_tools_audit_and_timeout_fault(paths, store, dataset):
    sid = _session(paths, store, dataset)
    server = build_server(ToolGateway(paths, store, sid), faults=["timeout_after_commit"])

    async def scenario():
        async with Client(server) as c:
            tools = {t.name: (t.description or "") for t in (await c.list_tools()).tools}
            names = set(tools)
            assert {"submit_experiment", "get_experiment", "write_report", "inspect_dataset"} <= names
            assert '{"lookback": 24}' in tools["submit_experiment"]
            assert '[{"artifact": "/workspace/momentum.py"' in tools["register_hypothesis"]
            assert "fills summary" in tools["get_experiment"] and "equity series" in tools["get_experiment"]
            args = {"artifact": "momentum", "parameters": {"lookback": 24}, "idempotency_key": "k1"}
            first = await c.call_tool("submit_experiment", args)
            assert first.is_error and "timeout" in first.content[0].text
            retry = await c.call_tool("submit_experiment", args)
            assert not retry.is_error
            out = _payload(retry)
            assert out["replayed"] and out["status"] == "completed"
            bad = await c.call_tool("submit_experiment", {**args, "idempotency_key": "k2", "segment": "holdout"})
            assert bad.is_error and "not available" in bad.content[0].text
            return out["experiment_id"]

    exp_id = asyncio.run(scenario())
    assert [e["id"] for e in store.list_experiments(limit=10, session_id=sid)] == [exp_id]
    events = [e["event"] for e in store.session_events(sid)]
    assert events.count("fault_injected") == 1 and "tool_refused" in events and events.count("tool_call") >= 2


def test_http_transport_requires_session_token(paths, store, dataset):
    sid = _session(paths, store, dataset)
    with GatewayHTTPServer(paths, store, sid) as srv:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "0"}}}
        headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
        assert httpx.post(srv.url(), json=init, headers=headers).status_code == 401
        wrong = {**headers, "authorization": "Bearer nope"}
        assert httpx.post(srv.url(), json=init, headers=wrong).status_code == 401
        ok = httpx.post(srv.url(), json=init, headers={**headers, "authorization": f"Bearer {srv.token}"})
        assert ok.status_code == 200 and "quant-research" in ok.text
        cfg = srv.client_config()["mcpServers"]["quant"]
        assert cfg["url"].startswith("http://host.docker.internal:") and srv.token in cfg["headers"]["Authorization"]
