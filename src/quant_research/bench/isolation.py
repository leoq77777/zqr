"""Isolation probe: from inside an agent container on the per-run network, try every way out.

Passing means: no direct internet, no DNS, no route to the host or the gateway's bind address, the proxy refuses
everything but the model host, and the gateway answers only through the proxy and only with its bearer token.
Used by `zqr desk check` (the research loop does not start until it passes) and by tests/test_egress.py.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
from pathlib import Path
from typing import Any

from quant_research.bench.egress import GATEWAY_PORT, PROXY_ALIAS, EgressNetwork, summarize
from quant_research.desk.config import NetworkConfig, container_network
from quant_research.experiments.store import ExperimentStore
from quant_research.gateway.mcp_server import GatewayHTTPServer
from quant_research.paths import Paths
from quant_research.runtime import agent_image

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
  const port = Number(process.env.GW_PORT), model = process.env.MODEL_HOST + ":" + process.env.MODEL_PORT;
  try { await fetch("https://example.com", {signal: AbortSignal.timeout(4000)}); out.direct_https = "reached"; }
  catch (e) { out.direct_https = "blocked:" + ((e.cause && e.cause.code) || e.name); }
  out.direct_ip = await tcp("1.1.1.1", 443);
  out.host_gateway = await tcp("host.docker.internal", port);
  out.gateway_bind = await tcp(process.env.GW_HOST, port);
  const own = Object.values(require("os").networkInterfaces()).flat().find((i) => i.family === "IPv4" && !i.internal);
  out.bridge_ip = await tcp(own.address.replace(/\.\d+$/, ".1"), port);
  out.desktop_host_ip = await tcp("192.168.65.254", port);
  try { await dns.lookup("example.com"); out.dns = "resolved"; } catch (e) { out.dns = "blocked:" + e.code; }
  out.connect_denied = await tcp("egress", 3128, "CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n");
  out.plain_http = await tcp("egress", 3128, "GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n");
  out.connect_allowed = await tcp("egress", 3128, "CONNECT " + model + " HTTP/1.1\r\nHost: " + model + "\r\n\r\n");
  const body = JSON.stringify({jsonrpc: "2.0", id: 1, method: "tools/list", params: {}});
  const hdr = {"content-type": "application/json", accept: "application/json, text/event-stream"};
  const post = (h) => fetch("http://egress:8080/mcp", {method: "POST", headers: h, body})
    .then((r) => r.status, (e) => "error:" + ((e.cause && e.cause.code) || e.name));
  out.gateway_no_token = await post(hdr);
  out.gateway_token = await post({...hdr, authorization: "Bearer " + process.env.GW_TOKEN});
  console.log(JSON.stringify(out));
})();
"""


def _iface_ipv4(name: str) -> str | None:
    if platform.system() != "Linux":
        return None
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", name], capture_output=True, text=True)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out.stdout)
    return m.group(1) if m else None


def candidate_networks() -> list[NetworkConfig]:
    """Release container: the control network only. Development, in order of preference: Docker Desktop host
    alias; WSL/host eth0; native Docker Engine's docker0."""
    container = container_network()
    if container:
        return [container]
    out = [NetworkConfig()]
    eth0 = _iface_ipv4("eth0")
    if eth0:
        out.append(NetworkConfig(mode="eth0", gateway_bind=eth0, forward_host=eth0))
    docker0 = _iface_ipv4("docker0")
    if docker0:
        out.append(NetworkConfig(mode="docker0", gateway_bind=docker0, forward_host=docker0))
    return out


def problems(res: dict[str, Any], egress: dict[str, Any]) -> list[str]:
    out = []
    if not str(res.get("direct_https", "")).startswith("blocked"):
        out.append(f"direct https: {res.get('direct_https')}")
    for key in ("direct_ip", "host_gateway", "gateway_bind", "bridge_ip", "desktop_host_ip"):
        if res.get(key) == "connected":
            out.append(f"{key} reachable from the agent container")
    if not str(res.get("dns", "")).startswith("blocked"):
        out.append(f"dns: {res.get('dns')}")
    if "403" not in str(res.get("connect_denied")) or "403" not in str(res.get("plain_http")):
        out.append(f"proxy did not refuse other hosts: {res.get('connect_denied')} / {res.get('plain_http')}")
    if "200" not in str(res.get("connect_allowed")):
        out.append(f"model host tunnel failed: {res.get('connect_allowed')}")
    if res.get("gateway_no_token") != 401:
        out.append(f"gateway without token answered {res.get('gateway_no_token')} (expected 401)")
    if res.get("gateway_token") != 200:
        out.append(f"gateway through the proxy answered {res.get('gateway_token')} (expected 200)")
    if egress.get("gateway_connections", 0) < 2:
        out.append("proxy recorded fewer than 2 gateway connections")
    return out


def run_probe(paths: Paths, store: ExperimentStore, session_id: str, network: NetworkConfig,
              model: tuple[str, int], workdir: Path, image: str | None = None) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)
    target = network.forward_host if network.gateway_bind == "0.0.0.0" else network.gateway_bind
    try:
        with GatewayHTTPServer(paths, store, session_id, host=network.gateway_bind,
                               extra_hosts=(f"{PROXY_ALIAS}:{GATEWAY_PORT}",)) as srv, \
                EgressNetwork(workdir / "egress", [model], srv.port, forward_host=network.forward_host,
                              control_network=network.control_network) as net:
            env = {**net.env(), "GW_PORT": str(srv.port), "GW_TOKEN": srv.token, "GW_HOST": target,
                   "MODEL_HOST": model[0], "MODEL_PORT": str(model[1])}
            cmd = ["docker", "run", "--rm", "-i", *net.docker_args()]
            for k, v in env.items():
                cmd += ["-e", f"{k}={v}"]
            out = subprocess.run([*cmd, image or agent_image(), "node", "-"], input=PROBE, capture_output=True,
                                 text=True, timeout=180)
            records = net.records()
    except Exception as exc:  # noqa: BLE001
        return {"network": network.model_dump(), "ok": False, "problems": [f"{type(exc).__name__}: {exc}"]}
    try:
        res = json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"network": network.model_dump(), "ok": False,
                "problems": [f"probe produced no result: {out.stderr.strip()[-300:]}"]}
    egress = summarize(records)
    found = problems(res, egress)
    return {"network": network.model_dump(), "ok": not found, "problems": found, "probe": res,
            "egress": {k: egress[k] for k in ("gateway_connections", "model_tunnels")} | {"denied": egress["denied"]}}
