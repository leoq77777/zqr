"""The agent container cannot reach anything except the model host (through the proxy) and the tool gateway.

Needs Docker and the agent/sandbox images; the allowed-tunnel case also needs internet access.
"""

import json
import shutil
import subprocess

import pytest

from quant_research.bench.adapter import AGENT_IMAGE
from quant_research.bench.egress import EgressNetwork, summarize
from quant_research.gateway.mcp_server import GatewayHTTPServer
from tests.test_mcp_gateway import _session

PROBE = r"""
const net = require("net"), dns = require("dns").promises;
const out = {};
function tcp(host, port, payload) {
  return new Promise((resolve) => {
    const s = net.connect({host, port, timeout: 4000});
    let buf = "";
    s.on("connect", () => { if (payload) s.write(payload); else { s.destroy(); resolve("connected"); } });
    s.on("data", (d) => { buf += d; if (buf.includes("\r\n")) { s.destroy(); resolve(buf.split("\r\n")[0]); } });
    s.on("timeout", () => { s.destroy(); resolve("timeout"); });
    s.on("error", (e) => resolve("error:" + e.code));
  });
}
(async () => {
  try { await fetch("https://example.com", {signal: AbortSignal.timeout(4000)}); out.direct_https = "reached"; }
  catch (e) { out.direct_https = "blocked:" + ((e.cause && e.cause.code) || e.name); }
  out.direct_ip = await tcp("1.1.1.1", 443);
  out.host_gateway = await tcp("host.docker.internal", Number(process.env.GW_PORT));
  const own = Object.values(require("os").networkInterfaces()).flat().find((i) => i.family === "IPv4" && !i.internal);
  out.bridge_ip = await tcp(own.address.replace(/\.\d+$/, ".1"), Number(process.env.GW_PORT));
  out.desktop_host_ip = await tcp("192.168.65.254", Number(process.env.GW_PORT));
  try { await dns.lookup("example.com"); out.dns = "resolved"; } catch (e) { out.dns = "blocked:" + e.code; }
  out.connect_denied = await tcp("egress", 3128, "CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n");
  out.plain_http = await tcp("egress", 3128, "GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n");
  out.connect_allowed = await tcp("egress", 3128,
    "CONNECT api.deepseek.com:443 HTTP/1.1\r\nHost: api.deepseek.com:443\r\n\r\n");
  const body = JSON.stringify({jsonrpc: "2.0", id: 1, method: "tools/list", params: {}});
  const hdr = {"content-type": "application/json", accept: "application/json, text/event-stream"};
  const post = (h) => fetch("http://egress:8080/mcp", {method: "POST", headers: h, body})
    .then((r) => r.status, (e) => "error:" + ((e.cause && e.cause.code) || e.name));
  out.gateway_no_token = await post(hdr);
  out.gateway_token = await post({...hdr, authorization: "Bearer " + process.env.GW_TOKEN});
  console.log(JSON.stringify(out));
})();
"""


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    ok = subprocess.run(["docker", "image", "inspect", AGENT_IMAGE], capture_output=True).returncode == 0
    return ok and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="docker or agent image unavailable")
def test_agent_network_only_reaches_model_host_and_gateway(paths, store, dataset, tmp_path):
    sid = _session(paths, store, dataset)
    probe = tmp_path / "probe.js"
    probe.write_text(PROBE)
    with GatewayHTTPServer(paths, store, sid, extra_hosts=("egress:8080",)) as srv, \
            EgressNetwork(tmp_path / "egress", [("api.deepseek.com", 443)], srv.port) as net:
        env = {**net.env(), "GW_PORT": str(srv.port), "GW_TOKEN": srv.token}
        cmd = ["docker", "run", "--rm", *net.docker_args(), "-v", f"{probe}:/probe.js:ro"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        out = subprocess.run([*cmd, AGENT_IMAGE, "node", "/probe.js"], capture_output=True, text=True, timeout=120)
        records = net.records()
    res = json.loads(out.stdout.strip().splitlines()[-1])
    print(json.dumps(res), json.dumps(summarize(records)))
    assert res["direct_https"].startswith("blocked"), res
    for key in ("direct_ip", "host_gateway", "bridge_ip", "desktop_host_ip"):
        assert res[key] != "connected", (key, res)
    assert res["dns"].startswith("blocked"), res
    assert "403" in res["connect_denied"] and "403" in res["plain_http"], res
    assert "200" in res["connect_allowed"], res
    assert res["gateway_no_token"] == 401 and res["gateway_token"] == 200, res
    s = summarize(records)
    assert any("example.com:443" in d for d in s["denied"]) and any("GET example.com" in d for d in s["denied"])
    assert s["gateway_connections"] >= 2
