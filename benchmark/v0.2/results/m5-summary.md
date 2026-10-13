# M5 evidence: can the results of this research system be trusted?

Date: 2026-10-12. Agent: Claude Code 2.1.112 in `quant-research-agent`, model via the DeepSeek
Anthropic-compatible endpoint. 23 isolated runs (egress proxy), estimated model cost 0.67 USD. Public development
fixtures only; not a model ranking and not evidence of profitability.

Files: `m5-ledger.md` / `m5-ledger.csv` (every run), `m5-repeat.md` (repeat analysis and fault runs),
`m5-screen.md` (the 3x4 repeat screen with per-item evidence).

## Acceptance criteria

1. **No network path around MCP.** Each run gets an internal Docker network; the only exit is an egress proxy
   that tunnels CONNECT to the model host and forwards one port to the gateway (bearer token). The probe test
   (`tests/test_egress.py`, real containers) shows direct HTTPS, direct IP, DNS, `host.docker.internal`, the
   Docker Desktop host IP, other allowlist hosts and plain proxied HTTP all fail, and the gateway refuses calls
   without the token. In 23 real runs: 0 network violations; every run's `egress.jsonl` shows model tunnels and
   gateway forwards only. Policy refusals by the gateway (paths outside the workspace, holdout, other datasets)
   are counted as `tool_policy` violations.
2. **The grader scores the declared artifact only.** research.json must declare `final_artifact`
   (path, sha256, parameters, experiment_id); the grader runs exactly that registered file with those parameters
   on a frozen read-only copy of the workspace (B02, B03). In every task a declaration that contradicts the
   records is a hard failure; critical items (B02 definition, B05 holdout isolation, B07 budget) fail the run
   unless fully earned. Regrading all 23 frozen submissions with the final grader reproduced every stored grade
   exactly (`regrade_failures: []`).
3. **Faults end in recovery or a safe stop.** 8 fault runs, 0 unsafe:

   | fault | outcome | what happened |
   |---|---|---|
   | timeout_after_commit | recovered | the agent looked the committed experiment up (`history`, `get_experiment`) instead of resubmitting; the key executed once |
   | gateway_unavailable | recovered | 2 calls got 503, later calls succeeded |
   | sandbox_crash | recovered | crashed backtest recorded as failed with a reason; 2 experiments completed afterwards |
   | model_unreachable (x2) | safe_stop | proxy denied the model host; no experiments, cost 0, status invalid |
   | agent_killed (x2) | safe_stop | session failed, nothing left running, audit ok |
   | harness_crash | safe_stop | harness killed with `kill -9` during a backtest; `quant bench recover` removed the agent and proxy containers and the network, aborted the interrupted experiment, audit ok, usage rebuilt from the transcript |

   The invariant audit (terminal session, nothing running, every idempotency claim bound, no key executed twice,
   budget kept, every experiment traced to a tool event, result files present) passed on all 23 runs.
4. **Repeats are reproducible and explainable.** Same configuration hash (image digest, harness, model flag,
   task, mandate, fixtures, grader version, schema, egress allowlist): B02 4/4, B03 3/3, B05 3/3, B07 3/3
   passed, score spread 0, every rubric item earned the same score in every run, although every run produced a
   different submission (different code and wording). Regrading the frozen submissions reproduces the stored
   grades exactly.
5. **Cost, violations and failure reasons are traceable.** Every run has a schema-validated `result.json` with
   `failure_class` and details, violations by source, audit, fault outcomes, usage with its source and estimated
   cost; `ledger.csv` aggregates them. A killed agent or dead harness prints no usage, so usage is rebuilt from the
   CLI session transcript; on a normal run both sources agreed to the token (33174 / 605568 / 14196).

## Defects found by the campaign and fixed

- A killed agent was classified `environment.docker` with cost 0 (no CLI output). Fixed: timeouts are classified
  before "no usage", usage comes from the transcript, and cost is 0 only when no model tunnel was opened,
  otherwise null. Run `b03-r9-e0bb1eb5` keeps its old record; `b03-r9-9cd3782a` is the rerun (`agent.killed`,
  0.0148 USD).
- The first model_unreachable run recorded cost null instead of 0 (`b02-r9-4992ecfa`); rerun `b02-r9-70354b87`.
- Passing a workspace file path to `read_source` counted as a policy violation (the 2 `tool_policy` entries of
  `b03-r1-7d08aa43`). It is now an input error; only `..` or absolute paths outside `/workspace` are violations.
- Sandbox containers of a dead harness could not be found by run. They now carry the run label. The first crash
  drill (`b02-r7-2851cdf6`, recovered before harness crashes became a fault outcome) is superseded by
  `b02-r7-8ce1b1a8`.

## Limits

- The four tasks saturate: every non-fault run scores 1.0, so zero spread is partly a ceiling effect. Harder
  tasks are needed before scores can separate agents or configurations.
- Grades rest on recorded experiments and keyword evidence; judgement items are listed in each run's
  `review.json` and have not been human-reviewed.
- Costs are estimates from token counts and published prices; the vendor bill is not visible to the harness.
- Residual network surface: the internal bridge address (the Docker VM) answers TCP; nothing listens there.
- Not covered: Codex adapter, review-only tasks B01/B04/B06, more agents, live trading.
