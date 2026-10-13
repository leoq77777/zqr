"""Low-intensity Coinbase Advanced Trade WebSocket observation (engineering readiness, not profitability).

Records heartbeat cadence, message gaps, sequence gaps, disconnects and reconnects to JSONL + summary.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import websockets

WS_URL = "wss://advanced-trade-ws.coinbase.com"


class WsStats:
    def __init__(self) -> None:
        self.started = time.time()
        self.messages = 0
        self.connects = 0
        self.disconnects = 0
        self.sequence_gaps = 0
        self.max_interval = 0.0
        self.last_msg: float | None = None
        self.errors: list[str] = []
        self.channels: dict[str, int] = {}

    def summary(self) -> dict[str, Any]:
        return {"started": self.started, "duration_s": time.time() - self.started, "messages": self.messages,
                "connects": self.connects, "disconnects": self.disconnects, "sequence_gaps": self.sequence_gaps,
                "max_message_interval_s": self.max_interval, "channels": self.channels,
                "errors": self.errors[-20:]}


async def observe(product_ids: list[str], duration_s: float, out_dir: Path, url: str = WS_URL) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = WsStats()
    deadline = time.time() + duration_s
    backoff = 1.0
    with open(out_dir / "ws_events.jsonl", "a") as log:
        while time.time() < deadline:
            try:
                async with websockets.connect(url, open_timeout=15, ping_interval=20) as ws:
                    stats.connects += 1
                    backoff = 1.0
                    for channel in ("heartbeats", "ticker"):
                        await ws.send(json.dumps({"type": "subscribe", "product_ids": product_ids,
                                                  "channel": channel}))
                    last_seq: int | None = None
                    while time.time() < deadline:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, min(30.0, deadline - time.time())))
                        now = time.time()
                        msg = json.loads(raw)
                        stats.messages += 1
                        ch = msg.get("channel", "?")
                        stats.channels[ch] = stats.channels.get(ch, 0) + 1
                        if stats.last_msg is not None:
                            stats.max_interval = max(stats.max_interval, now - stats.last_msg)
                        stats.last_msg = now
                        seq = msg.get("sequence_num")
                        if isinstance(seq, int):
                            if last_seq is not None and seq != last_seq + 1:
                                stats.sequence_gaps += 1
                                log.write(json.dumps({"recv": now, "event": "sequence_gap", "expected": last_seq + 1,
                                                      "got": seq}) + "\n")
                            last_seq = seq
                        if ch in ("heartbeats", "subscriptions") or msg.get("type") == "error":
                            log.write(json.dumps({"recv": now, "event": ch, "msg": msg}) + "\n")
            except (TimeoutError, websockets.WebSocketException, OSError) as exc:
                if time.time() >= deadline:
                    break
                stats.disconnects += 1
                stats.errors.append(f"{time.time():.0f} {type(exc).__name__}: {exc}")
                log.write(json.dumps({"recv": time.time(), "event": "disconnect", "error": str(exc)}) + "\n")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
    summary = stats.summary()
    (out_dir / "ws_summary.json").write_text(json.dumps(summary, indent=2))
    return summary
