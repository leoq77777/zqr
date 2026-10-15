"""`zqr memory ...` subcommands. Every step is deterministic and idempotent; `run` does them all in order."""

from __future__ import annotations

import json

from quant_research.experiments.store import ExperimentStore
from quant_research.paths import get_paths
from quant_research.strategy.sandbox import SandboxConfig


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _open(args):
    paths = get_paths(args.home)
    return paths, ExperimentStore(paths.db)


def step_backfill(paths, store, args) -> dict:
    from quant_research.memory.returns import backfill

    return backfill(paths, store)


def step_characterise(paths, store, args) -> dict:
    from quant_research.memory.characterise import characterise

    runs = characterise(paths, store)
    return {"runs": len(runs), "statuses": {s: sum(r["status"] == s for r in runs)
                                            for s in sorted({r["status"] for r in runs})}}


def step_cluster(paths, store, args) -> dict:
    from quant_research.memory import clusters
    from quant_research.memory.families import map_clusters, propose, tag_trials
    from quant_research.memory.returns import ReturnArchive

    tags = tag_trials(paths, store)
    out = {"tags": dict(tags)}
    for split_id in ReturnArchive(paths, store).splits():
        c = clusters.compute(paths, store, split_id)
        cid = clusters.record(store, c)
        mapping = map_clusters(store, cid)
        out[split_id] = {"clustering": cid, "k": c.k, "counts": c.counts,
                         "proposals_created": len(propose(store, split_id, cid, mapping))}
    return out


def step_import(paths, store, args) -> dict:
    from quant_research.memory.conclusions import import_conclusions

    return import_conclusions(paths, store)


def step_archive(paths, store, args) -> dict:
    from quant_research.memory.archive import write_archives

    files = write_archives(paths, store, sandbox=SandboxConfig(isolation=args.sandbox))
    return {"files": [str(p) for p in files]}


def step_report(paths, store, args) -> dict:
    from quant_research.memory.report import write_report

    return {"report": str(write_report(paths, store))}


STEPS = {"backfill": step_backfill, "characterise": step_characterise, "cluster": step_cluster,
         "import-conclusions": step_import, "archive": step_archive, "report": step_report}


def cmd_step(args) -> None:
    paths, store = _open(args)
    try:
        _out(STEPS[args.mcmd](paths, store, args))
    finally:
        store.close()


def cmd_run(args) -> None:
    paths, store = _open(args)
    try:
        _out({name: fn(paths, store, args) for name, fn in STEPS.items()})
    finally:
        store.close()


def cmd_status(args) -> None:
    from quant_research.memory import clusters
    from quant_research.memory.returns import ReturnArchive

    paths, store = _open(args)
    try:
        out = {}
        for split_id in ReturnArchive(paths, store).splits():
            last = clusters.latest(store, split_id)
            out[split_id] = {"archived": len(ReturnArchive(paths, store).index(split_id)),
                             "trials": store.trial_count(split_id=split_id),
                             "k": last["k"] if last else None, "clustered_at": last["computed_at"] if last else None}
            if (parent := store.split_parent(split_id)) is not None:
                kb = clusters.k_breakdown(store, split_id)
                out[split_id] |= {"parent": parent["parent_id"],
                                  "trials_inherited": store.split_trial_count(split_id)["inherited"],
                                  "k_inherited": kb["inherited"] if kb else None,
                                  "k_total": kb["total"] if kb else None}
        _out(out)
    finally:
        store.close()


def cmd_proposals(args) -> None:
    from quant_research.memory.families import proposals

    paths, store = _open(args)
    try:
        rows = proposals(store, status=None if args.status == "all" else args.status)
        _out([{"id": p["id"], "split_id": p["split_id"], "status": p["status"], "size": p["body"]["size"],
               "topics": p["body"]["topics"], "median_sharpe": p["body"]["median_sharpe"], "reason": p["reason"]}
              for p in rows])
    finally:
        store.close()


def cmd_reject(args) -> None:
    from quant_research.memory.families import reject_proposal

    paths, store = _open(args)
    try:
        for pid in args.ids:
            try:
                reject_proposal(store, pid, args.reason)
            except ValueError as exc:
                raise SystemExit(str(exc)) from None
        _out({"rejected": args.ids, "reason": args.reason})
    finally:
        store.close()


def register(sub) -> None:
    mem = sub.add_parser("memory", help="research memory: return archive, clusters and K, family archives")
    mem = mem.add_subparsers(dest="mcmd", required=True)
    helps = {"backfill": "archive the daily returns of every dev trial on record",
             "characterise": "run each seed instance once on dev (kind seed; counts as a trial)",
             "cluster": "tag trials, cluster residuals, record K at 0.3/0.5/0.7, propose new families",
             "import-conclusions": "import the session reports into the append-only conclusions",
             "archive": "regenerate the family archives under library/archives",
             "report": "write the clustering report under library/reports"}
    for name, text in helps.items():
        p = mem.add_parser(name, help=text)
        if name == "archive":
            p.add_argument("--sandbox", choices=("docker", "subprocess"), default="docker",
                           help="isolation for plugin reruns (plugins are skipped under subprocess)")
        p.set_defaults(fn=cmd_step)
    p = mem.add_parser("run", help="all steps in order")
    p.add_argument("--sandbox", choices=("docker", "subprocess"), default="docker")
    p.set_defaults(fn=cmd_run)
    mem.add_parser("status", help="archived trials and the latest K per split").set_defaults(fn=cmd_status)
    p = mem.add_parser("proposals", help="new-family proposals")
    p.add_argument("--status", choices=("open", "rejected", "all"), default="open")
    p.set_defaults(fn=cmd_proposals)
    p = mem.add_parser("reject", help="reject new-family proposals (the human's decision; recorded append-only)")
    p.add_argument("ids", nargs="+")
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_reject)
