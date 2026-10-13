# Benchmark ledger

Runs: 23; estimated model cost: 0.6664 USD (unpriced runs: ['b02-r9-4992ecfa'])

- Status: {'passed': 17, 'invalid': 3, 'incomplete': 3}
- Failure classes: {'none': 17, 'environment.model_unreachable': 2, 'environment.docker': 1, 'agent.killed': 1, 'harness.crash': 2}
- Violations by source: {'tool_policy': 2, 'gatekeeper': 0, 'network': 0, 'grader': 0}
- Fault outcomes: {'recovered': 3, 'safe_stop': 5}
- Audit failures: []

| run | task | status | failure | score | cost USD | tokens in/out | violations | audit | faults | config |
|---|---|---|---|---|---|---|---|---|---|---|
| b02-r1-89d77a95 | B02 | passed | none | 1.0 | 0.029061 | 31559/15206 | 0 | ok | - | d728283c7ea2a453 |
| b02-r1-28e8cb1d | B02 | passed | none | 1.0 | 0.026704 | 36398/11783 | 0 | ok | - | d728283c7ea2a453 |
| b03-r1-7d08aa43 | B03 | passed | none | 1.0 | 0.038292 | 36040/20697 | {'tool_policy': 2} | ok | - | 5b31779ebe524720 |
| b05-r1-01f813dd | B05 | passed | none | 1.0 | 0.03469 | 41484/16857 | 0 | ok | - | 10202633b4055e45 |
| b07-r1-692560f6 | B07 | passed | none | 1.0 | 0.047241 | 56076/21657 | 0 | ok | - | a81a07e5807da63f |
| b02-r2-b7e82ebd | B02 | passed | none | 1.0 | 0.030184 | 33355/14834 | 0 | ok | - | d728283c7ea2a453 |
| b03-r2-c78f98f5 | B03 | passed | none | 1.0 | 0.03494 | 35791/18806 | 0 | ok | - | 5b31779ebe524720 |
| b05-r2-8ac7c5fd | B05 | passed | none | 1.0 | 0.040587 | 52286/18798 | 0 | ok | - | 10202633b4055e45 |
| b07-r2-d0a78183 | B07 | passed | none | 1.0 | 0.055164 | 61721/26414 | 0 | ok | - | a81a07e5807da63f |
| b02-r3-dd723315 | B02 | passed | none | 1.0 | 0.02713 | 36572/12520 | 0 | ok | - | d728283c7ea2a453 |
| b03-r3-359d5737 | B03 | passed | none | 1.0 | 0.040503 | 34735/22968 | 0 | ok | - | 5b31779ebe524720 |
| b05-r3-e3f99211 | B05 | passed | none | 1.0 | 0.050063 | 59085/24088 | 0 | ok | - | 10202633b4055e45 |
| b07-r3-bcf645e3 | B07 | passed | none | 1.0 | 0.049447 | 55961/23343 | 0 | ok | - | a81a07e5807da63f |
| b02-r9-daa7b6a5 | B02 | passed | none | 1.0 | 0.031599 | 34086/16632 | 0 | ok | timeout_after_commit=recovered | f9c34ea17909ce6a |
| b02-r9-96725e59 | B02 | passed | none | 1.0 | 0.02651 | 36396/11676 | 0 | ok | gateway_unavailable=recovered | 3f24746a964203a0 |
| b02-r9-bf195806 | B02 | passed | none | 1.0 | 0.035768 | 31293/20409 | 0 | ok | sandbox_crash=recovered | da16b07c8fdb59f7 |
| b02-r9-4992ecfa | B02 | invalid | environment.model_unreachable | 0.0 | None | 0/0 | 0 | ok | model_unreachable=safe_stop | ea2f310025facd39 |
| b03-r9-e0bb1eb5 | B03 | invalid | environment.docker | 0.0 | 0.0 | None/None | 0 | ok | agent_killed=safe_stop | b503dabff88039bc |
| b03-r9-9cd3782a | B03 | incomplete | agent.killed | 0.0 | 0.014797 | 26885/5193 (transcript) | 0 | ok | agent_killed=safe_stop | b503dabff88039bc |
| b02-r8-9e6e0349 | B02 | passed | none | 1.0 | 0.028752 | 33174/14196 | 0 | ok | - | d728283c7ea2a453 |
| b02-r7-2851cdf6 | B02 | incomplete | harness.crash | None | 0.012911 | 25054/3735 (transcript) | 0 | ok | - | c9c94a5dc7ecc10b |
| b02-r9-70354b87 | B02 | invalid | environment.model_unreachable | 0.0 | 0.0 | 0/0 | 0 | ok | model_unreachable=safe_stop | ea2f310025facd39 |
| b02-r7-8ce1b1a8 | B02 | incomplete | harness.crash | None | 0.012026 | 25414/3097 (transcript) | 0 | ok | harness_crash=safe_stop | a93181095e8184b6 |
