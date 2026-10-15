"""`zqr inbox ...` subcommands. `add`, `retry` and `discard` are the human's; the research loop drafts items."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from quant_research.experiments.store import ExperimentStore
from quant_research.paths import get_paths


def _run(args, fn) -> None:
    from quant_research.inbox.store import InboxError

    paths = get_paths(args.home)
    store = ExperimentStore(paths.db)
    try:
        print(json.dumps(fn(paths, store), indent=2, default=str, ensure_ascii=False))
    except InboxError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        store.close()


def _summary(row) -> dict:
    return {k: row[k] for k in ("id", "state", "product", "url", "published", "note", "campaign_ids", "reason",
                                "cost_usd")} | {"text": row["text"][:120] + ("…" if len(row["text"]) > 120 else "")}


def cmd_add(args) -> None:
    from quant_research.inbox.store import InboxStore

    def go(paths, store):
        text = Path(args.file).read_text() if args.file else sys.stdin.read()
        return _summary(InboxStore(store).add(paths, text, args.url, args.published, args.product, args.note))

    _run(args, go)


def cmd_list(args) -> None:
    from quant_research.inbox.store import InboxStore

    _run(args, lambda paths, store: [_summary(r) for r in InboxStore(store).list(args.state)])


def cmd_show(args) -> None:
    from quant_research.campaign.store import CampaignStore
    from quant_research.inbox.store import InboxStore

    def go(paths, store):
        inbox = InboxStore(store)
        row = inbox.require(args.item_id)
        cs = CampaignStore(store)
        camps = [{"id": c["id"], "product": c["product"], "family": c["family"], "state": c["state"],
                  "goal": c["body"]["goal"], "prior": c["body"]["prior"],
                  "published": c["source"].get("published"),
                  "may_have_seen_validation": c["source"].get("may_have_seen_validation")}
                 for c in (cs.get(cid) for cid in row["campaign_ids"]) if c is not None]
        return {k: v for k, v in row.items() if k != "text_hash"} | {"campaigns": camps,
                                                                      "events": inbox.events(row["id"])}

    _run(args, go)


def cmd_retry(args) -> None:
    from quant_research.inbox.store import InboxStore

    _run(args, lambda paths, store: _summary(InboxStore(store).transition(args.item_id, "received", "human",
                                                                          args.note)))


def cmd_discard(args) -> None:
    from quant_research.inbox.store import InboxStore

    _run(args, lambda paths, store: _summary(InboxStore(store).transition(args.item_id, "discarded", "human",
                                                                          args.note)))


def register(sub) -> None:
    from quant_research.inbox.store import STATES

    ip = sub.add_parser("inbox", help="strategy inbox: external text drafted into proposed campaigns")
    ip = ip.add_subparsers(dest="icmd", required=True)
    p = ip.add_parser("add", help="store a text (from --file or standard input); the research loop drafts it")
    p.add_argument("--file", type=Path)
    p.add_argument("--url", help="where the text was published (kept as the citation, never fetched)")
    p.add_argument("--published", help="publication date YYYY-MM-DD")
    p.add_argument("--product", help="restrict the proposals to one product")
    p.add_argument("--note", help="your note for the drafting agent")
    p.set_defaults(fn=cmd_add)
    p = ip.add_parser("list")
    p.add_argument("--state", choices=STATES)
    p.set_defaults(fn=cmd_list)
    p = ip.add_parser("show", help="text, draft, campaigns written, publication warning, events, cost")
    p.add_argument("item_id")
    p.set_defaults(fn=cmd_show)
    p = ip.add_parser("retry", help="failed -> received (the human's)")
    p.add_argument("item_id")
    p.add_argument("--note", default="retry")
    p.set_defaults(fn=cmd_retry)
    p = ip.add_parser("discard", help="received -> discarded (the human's)")
    p.add_argument("item_id")
    p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_discard)
