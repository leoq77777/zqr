"""`zqr portfolio ...` subcommands (design 5.9). `seed` and `validate` act as the controller; `freeze`, `approve`
and `resume` are the human's decisions and need a note; `holdout` acts as the evaluator."""

from __future__ import annotations

import json

from quant_research.experiments.store import ExperimentStore
from quant_research.paths import get_paths


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _open(args):
    paths = get_paths(args.home)
    return paths, ExperimentStore(paths.db)


def _run(args, fn) -> None:
    from quant_research.portfolio.version import VersionError
    from quant_research.validation.holdout import HoldoutError

    paths, store = _open(args)
    try:
        _out(fn(paths, store))
    except (VersionError, HoldoutError) as exc:
        raise SystemExit(str(exc)) from None
    finally:
        store.close()


def cmd_seed(args) -> None:
    from quant_research.portfolio.blend import member_shares
    from quant_research.portfolio.seed import define_seed
    from quant_research.portfolio.version import VersionStore

    def go(paths, store):
        vid = define_seed(paths, store, args.product)
        return {"version_id": vid, "state": VersionStore(store).get(vid)["state"],
                "shares": member_shares(VersionStore(store).version(vid))}
    _run(args, go)


def cmd_validate(args) -> None:
    from quant_research.portfolio.evaluate import validate

    _run(args, lambda paths, store: validate(paths, store, args.version_id))


def cmd_show(args) -> None:
    from quant_research.portfolio.blend import member_shares
    from quant_research.portfolio.version import VersionError, VersionStore

    def go(paths, store):
        vs = VersionStore(store)
        row = vs.get(args.version_id)
        if row is None:
            raise VersionError(f"unknown portfolio version {args.version_id}")
        return {**row, "shares": member_shares(vs.version(args.version_id)), "events": vs.events(args.version_id)}
    _run(args, go)


def cmd_list(args) -> None:
    from quant_research.portfolio.version import VersionStore

    def go(paths, store):
        return [{k: r[k] for k in ("id", "product", "state", "split_id", "created_at", "updated_at")}
                for r in VersionStore(store).list(product=args.product, state=args.state)]
    _run(args, go)


def cmd_freeze(args) -> None:
    from quant_research.portfolio.version import VersionStore

    _run(args, lambda paths, store: VersionStore(store).transition(args.version_id, "frozen", "human",
                                                                   note=args.note))


def cmd_holdout(args) -> None:
    from quant_research.validation.holdout import run_portfolio_holdout

    _run(args, lambda paths, store: run_portfolio_holdout(paths, store, args.version_id, args.override_reason))


def register(sub) -> None:
    pf = sub.add_parser("portfolio", help="portfolio versions: seed, validate, freeze, holdout, approve, resume")
    pf = pf.add_subparsers(dest="pcmd", required=True)
    p = pf.add_parser("seed", help="define the seed portfolio of a product from the library (a draft version)")
    p.add_argument("--product", required=True)
    p.set_defaults(fn=cmd_seed)
    p = pf.add_parser("validate", help="dev and validation runs; draft -> validated | failed_validation")
    p.add_argument("version_id")
    p.set_defaults(fn=cmd_validate)
    p = pf.add_parser("show", help="a version: body, member shares, state, evidence and events")
    p.add_argument("version_id")
    p.set_defaults(fn=cmd_show)
    p = pf.add_parser("list", help="portfolio versions")
    p.add_argument("--product")
    p.add_argument("--state")
    p.set_defaults(fn=cmd_list)
    p = pf.add_parser("freeze", help="validated -> frozen (the human's decision)")
    p.add_argument("version_id")
    p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_freeze)
    p = pf.add_parser("holdout", help="the holdout test of a frozen version (gate 3; spends a viewing)")
    p.add_argument("version_id")
    p.add_argument("--override-reason", help="lift the viewing cap or the 7-day rule; contaminates the holdout")
    p.set_defaults(fn=cmd_holdout)
