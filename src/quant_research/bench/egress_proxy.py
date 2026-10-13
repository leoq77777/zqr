"""Egress proxy for the agent network. Runs alone in its own container; standard library only.

- HTTP CONNECT proxy: tunnels only to allowlisted host:port pairs; anything else gets 403.
- Fixed TCP forwards (`--forward LISTEN=HOST:PORT`): the tool gateway on the host.
- Every connection attempt is one JSON line in `--log` (host and port only, never payloads or URLs).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from urllib.parse import urlsplit

DENY = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    n = 0
    try:
        while data := await reader.read(65536):
            n += len(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()
    return n


class Proxy:
    def __init__(self, allow: set[tuple[str, int]], log_path: str):
        self.allow, self.log_path = allow, log_path

    def log(self, **rec) -> None:
        with open(self.log_path, "a") as f:
            f.write(json.dumps({"ts": round(time.time(), 3), **rec}) + "\n")

    async def _close(self, writer: asyncio.StreamWriter, payload: bytes = b"") -> None:
        try:
            if payload:
                writer.write(payload)
                await writer.drain()
            writer.close()
        except (ConnectionError, OSError):
            pass

    async def handle_proxy(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            await self._close(writer)
            return
        parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
        method, target = (parts + ["", ""])[:2]
        if method.upper() != "CONNECT":
            host = urlsplit(target).hostname or target[:80]
            self.log(kind="proxy", method=method[:16], host=host, port=urlsplit(target).port, allowed=False,
                     reason="only CONNECT tunnels to allowlisted hosts are permitted")
            await self._close(writer, DENY)
            return
        host, _, port_s = target.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            port = -1
        host = host.strip("[]").lower()
        if (host, port) not in self.allow:
            self.log(kind="proxy", method="CONNECT", host=host[:253], port=port, allowed=False,
                     reason="host not in allowlist")
            await self._close(writer, DENY)
            return
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, port), 15)
        except (TimeoutError, OSError) as exc:
            self.log(kind="proxy", method="CONNECT", host=host, port=port, allowed=True, error=type(exc).__name__)
            await self._close(writer, BAD_GATEWAY)
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        t0 = time.time()
        sent, received = await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
        self.log(kind="proxy", method="CONNECT", host=host, port=port, allowed=True, bytes_up=sent,
                 bytes_down=received, seconds=round(time.time() - t0, 3))

    def forwarder(self, listen: int, host: str, port: int):
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
            except (TimeoutError, OSError) as exc:
                self.log(kind="forward", listen=listen, allowed=True, error=type(exc).__name__)
                await self._close(writer)
                return
            sent, received = await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
            self.log(kind="forward", listen=listen, allowed=True, bytes_up=sent, bytes_down=received)
        return handle


async def main(args: argparse.Namespace) -> None:
    allow = set()
    for item in args.allow:
        h, _, p = item.rpartition(":")
        allow.add((h.lower(), int(p)))
    proxy = Proxy(allow, args.log)
    servers = [await asyncio.start_server(proxy.handle_proxy, "0.0.0.0", args.listen)]
    for spec in args.forward:
        listen, _, upstream = spec.partition("=")
        h, _, p = upstream.rpartition(":")
        servers.append(await asyncio.start_server(proxy.forwarder(int(listen), h, int(p)), "0.0.0.0", int(listen)))
    proxy.log(kind="start", allow=sorted(f"{h}:{p}" for h, p in allow), forwards=args.forward)
    print("egress ready", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=3128)
    ap.add_argument("--allow", action="append", default=[])
    ap.add_argument("--forward", action="append", default=[])
    ap.add_argument("--log", required=True)
    asyncio.run(main(ap.parse_args()))
