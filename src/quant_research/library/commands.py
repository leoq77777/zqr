"""`zqr library ...` subcommands."""

from __future__ import annotations

import json

from quant_research.library.catalog import LibraryError, load_catalog
from quant_research.library.store import STATUSES, LibraryStore
from quant_research.paths import get_paths


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _store(args) -> LibraryStore:
    return LibraryStore(get_paths(args.home).db)


def cmd_import(args) -> None:
    try:
        catalog = load_catalog(args.file)
        store = _store(args)
        try:
            result = store.plan_import(catalog) if args.dry_run else store.import_catalog(catalog)
        finally:
            store.close()
    except LibraryError as exc:
        raise SystemExit(str(exc)) from None
    _out({"dry_run": args.dry_run, **result})


def _summary(params: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in sorted(params.items()))


def cmd_list(args) -> None:
    store = _store(args)
    try:
        rows = store.entries(product=args.product, family=args.family, status=args.status)
    finally:
        store.close()
    if args.json:
        _out(rows)
        return
    if not rows:
        print("library is empty (import the seed catalog with `zqr library import`)")
        return
    print(f"{'id':<24} {'product':<8} {'family':<17} {'kind':<8} {'status':<10} strategy")
    for r in rows:
        print(f"{r['id']:<24} {r['product']:<8} {r['family']:<17} {r['kind']:<8} {r['status']:<10} "
              f"{r['strategy_ref'].split(':', 1)[1]} {_summary(r['parameters'])}")


def cmd_show(args) -> None:
    store = _store(args)
    try:
        row = store.entry(args.id)
        if row is None:
            raise SystemExit(f"unknown library entry {args.id!r}")
        _out({**row, "events": store.events(args.id)})
    finally:
        store.close()


def register(sub) -> None:
    lib = sub.add_parser("library", help="strategy library: seed catalog and entries").add_subparsers(
        dest="lcmd", required=True)
    p = lib.add_parser("import", help="import the reviewed seed catalog (adds new entries; never changes old ones)")
    p.add_argument("--file", help="catalog YAML (default: the packaged seeds.yaml)")
    p.add_argument("--dry-run", action="store_true", help="show what would be added, write nothing")
    p.set_defaults(fn=cmd_import)
    p = lib.add_parser("list")
    p.add_argument("--product")
    p.add_argument("--family")
    p.add_argument("--status", choices=STATUSES)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_list)
    p = lib.add_parser("show")
    p.add_argument("id")
    p.set_defaults(fn=cmd_show)
