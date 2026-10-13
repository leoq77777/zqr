# M5 repeat analysis

Runs grouped by task and configuration hash (same image digest, harness, model flag, task, mandate, fixtures, grader version, egress allowlist). Fault runs are reported separately.

## B02 (config `c9c94a5dc7ecc10b`)

- Runs: 1; passed: 0/1; score mean None, range [None, None], sd 0.0; mean cost 0.012911 USD
- Failure classes: {'harness.crash': 1}
- Distinct submissions: 1 of 1 runs
- Regrade of frozen submissions identical to the recorded grade: True
- Every rubric item earned the same score in every run.

## B02 (config `d728283c7ea2a453`)

- Runs: 5; passed: 5/5; score mean 1.0, range [1.0, 1.0], sd 0.0; mean cost 0.028366 USD
- Failure classes: {'none': 5}
- Distinct submissions: 5 of 5 runs
- Regrade of frozen submissions identical to the recorded grade: True
- Every rubric item earned the same score in every run.

## B03 (config `5b31779ebe524720`)

- Runs: 3; passed: 3/3; score mean 1.0, range [1.0, 1.0], sd 0.0; mean cost 0.037912 USD
- Failure classes: {'none': 3}
- Distinct submissions: 3 of 3 runs
- Regrade of frozen submissions identical to the recorded grade: True
- Every rubric item earned the same score in every run.

## B05 (config `10202633b4055e45`)

- Runs: 3; passed: 3/3; score mean 1.0, range [1.0, 1.0], sd 0.0; mean cost 0.04178 USD
- Failure classes: {'none': 3}
- Distinct submissions: 3 of 3 runs
- Regrade of frozen submissions identical to the recorded grade: True
- Every rubric item earned the same score in every run.

## B07 (config `a81a07e5807da63f`)

- Runs: 3; passed: 3/3; score mean 1.0, range [1.0, 1.0], sd 0.0; mean cost 0.050617 USD
- Failure classes: {'none': 3}
- Distinct submissions: 3 of 3 runs
- Regrade of frozen submissions identical to the recorded grade: True
- Every rubric item earned the same score in every run.

## Fault runs

| run | task | faults | status | failure | audit |
|---|---|---|---|---|---|
| b02-r9-daa7b6a5 | B02 | timeout_after_commit: **recovered** (key b02-mom24-01: 1 execution(s); retried with the same key: False) | passed | none | ok |
| b02-r9-96725e59 | B02 | gateway_unavailable: **recovered** (2 call(s) refused with 503, later calls succeeded) | passed | none | ok |
| b02-r9-bf195806 | B02 | sandbox_crash: **recovered** (crash recorded as failed; 2 completed experiment(s) afterwards) | passed | none | ok |
| b02-r9-4992ecfa | B02 | model_unreachable: **safe_stop** (status invalid, 0 experiments, cost None) | invalid | environment.model_unreachable | ok |
| b03-r9-e0bb1eb5 | B03 | agent_killed: **safe_stop** (session failed, status invalid, failure environment.docker) | invalid | environment.docker | ok |
| b03-r9-9cd3782a | B03 | agent_killed: **safe_stop** (session failed, status incomplete, failure agent.killed) | incomplete | agent.killed | ok |
| b02-r9-70354b87 | B02 | model_unreachable: **safe_stop** (status invalid, 0 experiments, cost 0.0) | invalid | environment.model_unreachable | ok |
| b02-r7-8ce1b1a8 | B02 | harness_crash: **safe_stop** (session failed, status incomplete, 1 interrupted experiment(s) aborted, containers/networks left after recover: []) | incomplete | harness.crash | ok |
