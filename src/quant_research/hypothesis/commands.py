"""`zqr hypothesis ...` subcommands. `register` is the human's (release 4c); `test` and `marginal` act as the
controller and may be run by anyone."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from quant_research.experiments.store import ExperimentStore
from quant_research.paths import get_paths


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _run(args, fn) -> None:
    from pydantic import ValidationError

    from quant_research.hypothesis.store import HypothesisError
    from quant_research.library.catalog import LibraryError
    from quant_research.portfolio.version import VersionError
    from quant_research.strategy.spec import StrategyError

    paths = get_paths(args.home)
    store = ExperimentStore(paths.db)
    try:
        _out(fn(paths, store))
    except (HypothesisError, LibraryError, VersionError, StrategyError, ValidationError) as exc:
        raise SystemExit(str(exc)) from None
    finally:
        store.close()


def _sandbox(args):
    from quant_research.strategy.sandbox import SandboxConfig

    return SandboxConfig(isolation="docker") if args.sandbox == "docker" else None


def load_file(paths, path: Path) -> dict:
    """The registration file; a variant given as `plugin_file` is frozen and replaced by `plugin:<sha256>`."""
    from quant_research.strategy.plugin import register_plugin

    body = yaml.safe_load(Path(path).read_text())
    if not isinstance(body, dict):
        raise ValueError(f"{path}: a mapping is expected")
    variants = []
    for v in body.get("variants") or []:
        v = dict(v)
        if "plugin_file" in v:
            src = Path(v.pop("plugin_file"))
            src = src if src.is_absolute() else Path(path).parent / src
            v["strategy_ref"] = "plugin:" + register_plugin(paths, src)
        variants.append(v)
    return body | {"variants": variants}


def cmd_register(args) -> None:
    from quant_research.hypothesis.model import Hypothesis
    from quant_research.hypothesis.store import HypothesisStore

    def go(paths, store):
        if not args.note.strip():
            raise SystemExit("a registration needs a note")
        h = Hypothesis.model_validate(load_file(paths, args.file))
        return HypothesisStore(store).register(paths, h, "human", args.note)
    _run(args, go)


def cmd_list(args) -> None:
    from quant_research.hypothesis.store import HypothesisStore

    def go(paths, store):
        return [{"id": r["id"], "product": r["product"], "family": r["family"], "state": r["state"],
                 "variants": len(r["body"]["variants"]), "split_id": r["split_id"], "registered_by": r["registered_by"],
                 "at": r["at"]} for r in HypothesisStore(store).list(product=args.product, state=args.state)]
    _run(args, go)


def cmd_show(args) -> None:
    from quant_research.hypothesis.store import HypothesisError, HypothesisStore

    def go(paths, store):
        hs = HypothesisStore(store)
        row = hs.get(args.hypothesis_id)
        if row is None:
            raise HypothesisError(f"unknown hypothesis {args.hypothesis_id}")
        return {**row, "events": hs.events(args.hypothesis_id)}
    _run(args, go)


def cmd_test(args) -> None:
    from quant_research.hypothesis.gates import run_gates

    _run(args, lambda paths, store: run_gates(paths, store, args.hypothesis_id, _sandbox(args)))


def cmd_marginal(args) -> None:
    from quant_research.hypothesis.gates import marginal

    _run(args, lambda paths, store: marginal(paths, store, args.experiment_id))


def register(sub) -> None:
    from quant_research.hypothesis.store import STATES

    hy = sub.add_parser("hypothesis", help="registered hypotheses and the admission gates (appendix A6, A13.7)")
    hy = hy.add_subparsers(dest="hcmd", required=True)
    p = hy.add_parser("register", help="register a hypothesis from a YAML file (the human's; immutable)")
    p.add_argument("--file", required=True, type=Path)
    p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_register)
    p = hy.add_parser("list")
    p.add_argument("--product")
    p.add_argument("--state", choices=STATES)
    p.set_defaults(fn=cmd_list)
    p = hy.add_parser("show", help="body, events and gate evidence")
    p.add_argument("hypothesis_id")
    p.set_defaults(fn=cmd_show)
    p = hy.add_parser("test", help="gate 1 on dev, then gate 2 on validation if a variant passed (controller)")
    p.add_argument("hypothesis_id")
    p.add_argument("--sandbox", choices=("docker", "none"), default="docker",
                   help="docker runs plugin variants (template variants run in process either way)")
    p.set_defaults(fn=cmd_test)
    p = hy.add_parser("marginal", help="gate 1's regression for any dev experiment, without a verdict (dev only)")
    p.add_argument("experiment_id")
    p.set_defaults(fn=cmd_marginal)
