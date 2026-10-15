# Design: strategy inbox

Status: approved in conversation on 2026-10-15; written for review before the implementation plan.

## 1. Problem

The human often reads strategies published by others (posts, threads, articles). Today the only way to research
one is to write a campaign YAML by hand and run `zqr campaign add`. The inbox takes the text as it is and has an
isolated agent draft campaign proposals from it, which the human then approves or ignores.

An external strategy is an idea, not evidence. Its published backtests are never used. A proposal from the inbox
is an ordinary `proposed` campaign: the same caps, family quotas, gates, holdout and paper probation as a
generated one. A strategy published recently was chosen by its author on data that may include our validation and
holdout periods, so every proposal records when the idea was published.

## 2. Decisions

| # | Decision |
|---|---|
| I1 | Input is plain text only. Links are kept as citations and never fetched; images and video are out of scope. |
| I2 | Submission is a CLI command on the production host. A 飞书 bot may reuse the same intake later; not in this design. |
| I3 | The inbox drafts campaign proposals only, never hypotheses or variants: the campaign's sessions design the variants. |
| I4 | Drafting is a short agent run in the research loop, in the same Docker isolation and egress allowlist as research sessions, with no experiment tools and no market data. |
| I5 | A proposal maps to an existing sleeve family or to `exploration`. The inbox never proposes new families (`zqr library family add` stays the human's). |
| I6 | Drafting writes campaigns in `proposed`, like `zqr campaign generate`; approval stays the human's (P5). No appendix change: to be recorded as a plan decision for the human to approve. |

## 3. Flow

1. `zqr inbox add` stores the text as an item in `received` and prints its id (`inb-<12 hex>`). No model call.
2. Each step of the research loop checks, in order: pending gates of the running campaign, then the inbox, then the
   next campaign session. The oldest `received` item gets a drafting run and moves to `drafting`.
3. The run ends the item in one of three states:
   - `drafted`: one or two campaigns written as `proposed` (two when no product was given and the idea applies to
     both BTC-USD and ETH-USD);
   - `infeasible`: the idea needs shorting, leverage, high turnover that 0.6% per side cannot carry, or data the
     desk does not have (funding rates, order books, on-chain or sentiment data); the reason is recorded and no
     campaign is written;
   - `failed`: the run produced no valid draft; `zqr inbox retry` puts it back in `received`.
4. The human reads the result with `zqr inbox show` and `zqr campaign show`, and queues proposals with the existing
   `zqr campaign approve`.

## 4. Record

Tables in the experiment store's SQLite database:

- `inbox_items`: `id`, `text`, `text_hash`, `url`, `published` (given by the human), `product` (optional),
  `note`, `state`, `draft` (the validated draft, JSON), `campaign_ids` (JSON), `reason`, `cost_usd`,
  `created_at`, `updated_at`.
- `inbox_events`: `item_id`, `event`, `actor`, `data`, `at`; one row per state change, written like
  `campaign_events`.

States: `received` → `drafting` → `drafted` | `infeasible` | `failed`; `failed` → `received` (human `retry`);
`received` → `discarded` (human `discard`).

Rules:

- Text is 1 to 20,000 characters after trimming. A text whose normalized hash (whitespace collapsed) matches an
  existing item is refused with that item's id.
- Each campaign written from an item has source `{"kind": "inbox", "inbox_id": ..., "published": ...,
  "may_have_seen_validation": ...}` and the citation `{kind: published, ref: <url>}`, or
  `{kind: published, ref: "inbox:<id>"}` without a URL. `campaign list` shows the source kind.
- Publication date: the human's `--published`, otherwise a date the agent found in the text, otherwise unknown.
  `may_have_seen_validation` is true when the date is on or after the validation start of the campaign's split, or
  unknown. It is a warning shown in `inbox show`, `campaign show` and the report, never a refusal.

## 5. Commands

| Command | Who | Effect |
|---|---|---|
| `zqr inbox add [--file F] [--url U] [--published YYYY-MM-DD] [--product P] [--note N]` | Human | Store an item; without `--file` the text is read from standard input |
| `zqr inbox list [--state S]` | Anyone | Items, states, campaigns written |
| `zqr inbox show <id>` | Anyone | Text, draft, reason, campaigns, publication warning, cost |
| `zqr inbox retry <id>` | Human | `failed` → `received` |
| `zqr inbox discard <id> --note N` | Human | `received` → `discarded` |

Production data lives on the Windows host. The host wrapper already passes standard input into the desk container
(`docker exec -i`), so from the Mac: `ssh quant-wsl 'zqr inbox add --url U' < post.txt`.

## 6. Drafting run

- **Isolation.** The research session's agent container behind the egress proxy (model endpoint only), with an
  MCP configuration that exposes no tools. No dataset, no experiment store and no gateway session are mounted or
  created. Run directory: `desk/inbox/<run_id>/`, with `run.json`, the frozen workspace and `result.json`.
- **Workspace**, written by the controller:
  - `TASK.md`: the job, the human's note, URL and product, and the text inside a block marked as untrusted external
    material: to be analysed, never followed.
  - `CONSTRAINTS.md`: long or flat spot only, no leverage; BTC-USD and ETH-USD hourly OHLCV and the dataset's date
    range; costs (0.6% per side plus slippage); the data the desk does not have.
  - `LIBRARY.md`: sleeve families and their theses; the family archives' agent-facing view (dev statistics and gate
    verdicts only, as research sessions see them); live and concluded campaigns with goals and verdicts.
  - `draft.schema.json`: the output schema.
- **Output** `draft.json`, one of:
  - `{"verdict": "infeasible", "reason": ...}`;
  - `{"verdict": "proposals", "published": <date or null>, "proposals": [...]}`: one or two proposals, each with
    `product`, `family`, `goal`, `prior` (the author's claim and the form in which it can be tested here) and
    `overlap` (what earlier conclusions already say about the idea, for example "ewma_trend refuted twice on this
    split").
- **Validation** by the controller: the schema, a known product (the human's `--product` wins), a family from
  `LIBRARY.md`, field lengths (`CampaignSpec`), then `CampaignStore.add` with its quota and pricing checks. A
  proposal refused there (for example a family at quota) is recorded with its reason in the item's draft; the other
  proposal is still written. An item with proposals but none written ends `drafted` with an empty `campaign_ids`
  and the reasons shown.

## 7. Safety

- The text is stored in the database and copied only into the drafting workspace. It never enters a research
  session's brief; research agents see the drafted goal and prior.
- The drafting agent can be misled by the text, for example into hiding instructions in a prior. The guards: the
  human reads the full proposal before approving it, and a research session runs in the same isolation with dev
  data only.
- The drafting agent sees no validation or holdout metric: `LIBRARY.md` is built from the same agent-facing views
  as research briefs.
- No model credential reaches the CLI: `inbox add` only writes to the database; the research child runs the agent.

## 8. Budget

- Each drafting run reserves `inbox_reserve_usd` (default $0.10) against the UTC day and the month, like a session
  reservation. It counts toward the daily ($5) and monthly ($83.33) ceilings, not toward any campaign or the 12
  sessions per day.
- At most `inbox_drafts_per_day` (default 10) drafting runs per UTC day; further items wait for the next day.
- A run costing more than its reservation, or a model call without a price, writes the budget stop as today.
- Ledger line in `desk/research_ledger.jsonl` with `topic: "inbox"` and `inbox_id`. The daily report's research
  section gets an inbox line: items received, drafted, infeasible and failed that day, their cost, and the
  proposals written.

## 9. Failures

| Failure | Item | Loop |
|---|---|---|
| Model unreachable, Docker error, harness error | back to `received` | pauses with backoff, as for sessions |
| No `draft.json`, invalid JSON or schema | `failed`, reason recorded | continues |
| Desk restarted or crashed during the run | back to `received` on recovery; cost booked from transcripts | continues |
| Budget stop | `drafted`/`failed` as the output decides; the stop blocks the next run | stops until cleared |

## 10. Tests

FakeAgent end to end:

- a draft with one proposal; with two proposals (both products); `infeasible`;
- invalid output; a proposal for a family at quota next to a valid one;
- duplicate text refused; `retry` and `discard` transitions and their refusals in other states;
- publication warning with a date after the validation start, before it, and unknown;
- a run over its reservation writes the budget stop; an unpriced model stops the loop;
- the daily drafting limit;
- the loop drafts before the next campaign session, and after pending gates;
- the text never appears in a research session's `TASK.md`;
- the report's inbox line.

Rehearsal on a copy of production data: one real post end to end, from `inbox add` to a `proposed` campaign.
