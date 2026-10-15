"""`zqr campaign ...` subcommands. `add`, `approve`, `cancel` and `budget-stop clear` are the human's (P5, D7);
`generate` acts as the controller and writes `proposed` campaigns only."""

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

from quant_research.experiments.store import ExperimentStore
from quant_research.paths import get_paths


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _run(args, fn) -> None:
    from pydantic import ValidationError

    from quant_research.campaign.store import CampaignError
    from quant_research.hypothesis.store import HypothesisError
    from quant_research.portfolio.version import VersionError

    paths = get_paths(args.home)
    store = ExperimentStore(paths.db)
    try:
        _out(fn(paths, store))
    except (CampaignError, HypothesisError, VersionError, ValidationError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    finally:
        store.close()


def cmd_list(args) -> None:
    from quant_research.campaign.store import CampaignStore

    def go(paths, store):
        cs = CampaignStore(store)
        return [{"id": r["id"], "product": r["product"], "family": r["family"], "state": r["state"],
                 "source": r["source"].get("kind"), "diagnostic": r["source"].get("diagnostic"),
                 "score": r["source"].get("score"), "goal": r["body"]["goal"], "usage": cs.usage(r["id"]),
                 "caps": cs.caps(r["id"])}
                for r in cs.list(args.product, args.state)]

    _run(args, go)


def cmd_show(args) -> None:
    from quant_research.campaign.conclude import campaign_hypothesis
    from quant_research.campaign.store import CampaignStore
    from quant_research.hypothesis.gates import verdict

    def go(paths, store):
        cs = CampaignStore(store)
        row = cs.require(args.campaign_id)
        hid = campaign_hypothesis(store, row["id"])
        concl = store.query("SELECT id, citations_ok, problems, body FROM family_conclusions WHERE session_id=?",
                            (f"campaign:{row['id']}",)) if store.query(
            "SELECT name FROM sqlite_master WHERE name='family_conclusions'") else []
        return row | {"caps": cs.caps(row["id"]), "usage": cs.usage(row["id"]), "events": cs.events(row["id"]),
                      "sessions": cs.sessions(row["id"]), "hypothesis": verdict(store, hid) if hid else None,
                      "conclusion": ({**concl[0], "body": json.loads(concl[0]["body"])} if concl else None)}

    _run(args, go)


def cmd_add(args) -> None:
    from quant_research.campaign.model import CampaignSpec
    from quant_research.campaign.store import CampaignStore

    def go(paths, store):
        body = yaml.safe_load(Path(args.file).read_text())
        if not isinstance(body, dict):
            raise ValueError(f"{args.file}: a mapping is expected")
        return CampaignStore(store).add(paths, CampaignSpec.model_validate(body), {"kind": "human", "note": args.note},
                                        "human")

    _run(args, go)


def cmd_approve(args) -> None:
    from quant_research.campaign.store import CampaignStore

    def go(paths, store):
        cs = CampaignStore(store)
        for cid in args.campaign_ids:
            cs.transition(cid, "queued", "human", args.note)
        return [{"id": cid, "state": cs.require(cid)["state"]} for cid in args.campaign_ids]

    _run(args, go)


def cmd_cancel(args) -> None:
    from quant_research.campaign.store import CampaignStore

    def go(paths, store):
        cs = CampaignStore(store)
        cs.transition(args.campaign_id, "cancelled", "human", args.note)
        return {"id": args.campaign_id, "state": "cancelled"}

    _run(args, go)


def cmd_generate(args) -> None:
    from quant_research.campaign.generate import batch, generate

    def go(paths, store):
        if args.dry_run:
            return [{k: c[k] for k in ("key", "family", "diagnostic", "gap", "room", "score")}
                    | {"goal": c["spec"]["goal"]} for c in batch(paths, store, args.product, args.n)]
        return generate(paths, store, args.product, args.n)

    _run(args, go)


def cmd_quotas(args) -> None:
    from quant_research.campaign.store import CampaignStore, desk_split

    _run(args, lambda paths, store: CampaignStore(store).quotas(paths, desk_split(paths, store, args.product)))


def cmd_budget(args) -> None:
    from quant_research.campaign import budget
    from quant_research.desk import research as rl
    from quant_research.desk.config import load_config

    def go(paths, store):
        if args.action == "clear":
            return budget.clear_stop(paths, args.note or "")
        cfg = load_config(paths)
        day = rl.utc_day(time.time())
        return {"limits": budget.limits(cfg), "day": day, "spent_day": rl.spent_on(paths, day),
                "spent_month": rl.spent_in_month(paths, budget.month_of(day)),
                "default_model": budget.default_model(cfg), "campaign_models": cfg.campaign_models,
                "stop": budget.active_stop(paths)}

    _run(args, go)


def register(sub) -> None:
    from quant_research.campaign.model import STATES

    cp = sub.add_parser("campaign", help="research campaigns: the reviewed queue of the research loop (design 6)")
    cp = cp.add_subparsers(dest="ccmd", required=True)
    p = cp.add_parser("list")
    p.add_argument("--product")
    p.add_argument("--state", choices=STATES)
    p.set_defaults(fn=cmd_list)
    p = cp.add_parser("show", help="record, events, sessions, caps and usage, hypothesis verdict, conclusion")
    p.add_argument("campaign_id")
    p.set_defaults(fn=cmd_show)
    p = cp.add_parser("add", help="add a campaign from a YAML file (the human's; enters `proposed`)")
    p.add_argument("--file", required=True, type=Path)
    p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_add)
    p = cp.add_parser("approve", help="proposed -> queued (the human's, P5)")
    p.add_argument("campaign_ids", nargs="+")
    p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_approve)
    p = cp.add_parser("cancel", help="cancel a proposed, queued or running campaign (the human's)")
    p.add_argument("campaign_id")
    p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_cancel)
    p = cp.add_parser("generate", help="deterministic proposals from dev diagnostics (written as `proposed`)")
    p.add_argument("--product", required=True)
    p.add_argument("--n", type=int, default=6)
    p.add_argument("--dry-run", action="store_true", help="print the batch without writing it")
    p.set_defaults(fn=cmd_generate)
    p = cp.add_parser("quotas", help="trials per family on the product's desk split against the quotas (P1)")
    p.add_argument("--product", required=True)
    p.set_defaults(fn=cmd_quotas)
    p = cp.add_parser("budget", help="limits, spend and the budget stop; `clear` is the human's (D7)")
    p.add_argument("action", nargs="?", choices=("status", "clear"), default="status")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_budget)
