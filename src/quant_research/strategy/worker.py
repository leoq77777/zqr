"""Runs inside the sandbox: python -m quant_research.strategy.worker <workdir>."""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import traceback
from pathlib import Path


def _block_network() -> None:
    # Best-effort guard for the subprocess backend; real isolation comes from the docker backend.
    def _denied(*_a, **_k):
        raise PermissionError("network access is disabled for strategy code")

    socket.socket = _denied  # type: ignore[assignment,misc]
    socket.create_connection = _denied  # type: ignore[assignment]
    socket.getaddrinfo = _denied  # type: ignore[assignment]


def _entry_point(module):
    """(callable taking a bars DataFrame, contract). v0.2 exposures first; legacy final-target entry points after."""
    if hasattr(module, "generate_exposures"):
        return module.generate_exposures, "exposures"
    if hasattr(module, "target_positions"):
        return module.target_positions, "legacy_targets"
    if hasattr(module, "generate_targets"):  # benchmark contract v0.1: list of dicts with `start` epoch seconds
        from quant_research.data.bars import to_epoch

        def call(df, params):
            rows = df[["open", "high", "low", "close", "volume"]].assign(
                start=to_epoch(df["bar_open_time"])).to_dict("records")
            return module.generate_targets(rows, params)
        return call, "legacy_targets"
    raise AttributeError("strategy module defines none of generate_exposures, target_positions, generate_targets")


def main(workdir: Path) -> int:
    import pandas as pd

    from quant_research.strategy.spec import normalize_targets

    _block_network()
    request = json.loads((workdir / "request.json").read_text())
    try:
        bars = pd.read_parquet(workdir / "bars.parquet")
        spec = importlib.util.spec_from_file_location("candidate_strategy", workdir / "strategy.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn, contract = _entry_point(module)
        strict = contract == "exposures"
        params = request["parameters"]
        full = normalize_targets(fn(bars.copy(), dict(params)), len(bars), strict)
        prefix = {}
        for n in request.get("prefix_lengths", []):
            part = normalize_targets(fn(bars.iloc[:n].copy(), dict(params)), n, strict)
            prefix[str(n)] = float(part[-1])
        pd.DataFrame({"target": full}).to_parquet(workdir / "targets.parquet", index=False)
        (workdir / "result.json").write_text(json.dumps({"ok": True, "prefix": prefix, "contract": contract}))
        return 0
    except Exception as exc:  # report candidate errors as data, not crashes
        (workdir / "result.json").write_text(json.dumps(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-4000:]}))
        return 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
