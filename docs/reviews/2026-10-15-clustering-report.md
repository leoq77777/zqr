# Clustering report 2026-10-15

Rules: `docs/contracts-v2-appendix-a.md` (A1-A5, A7, A12). Residual correlations after removing the coin's daily return; average linkage; K(0.7) is the effective number of trials used by the split DSR.

## BTC-USD (`split-1016c7c9873a`)

Trials: 896 (891 with returns, 5 without; 3 flat, 0 passive). Clustering input `72489c5b0547ac0f`.

| ρ threshold | 0.3 | 0.5 | 0.7 |
|---|---|---|---|
| K (winsorised, used) | 41 | 100 | **232** |
| K (not winsorised) | 37 | 90 | 217 |
| 2026-10-13 feasibility run, raw (winsorised) | 34 (43) | 113 (116) | 255 (267) |

K counts every cluster once, plus one for the flat cluster, one for the passive cluster and one per trial without returns (5). With K = 232 on T = 1267 days: SR0(K) = 1.51, σ0 = 0.54, so a split DSR of 0.9 needs an annualised dev Sharpe of at least 2.20 (more if fat tails widen σ_SR).

Cluster sizes (trials with returns, excluding flat and passive):

- ρ 0.3: 35 clusters; sizes 1: 9, 2-5: 12, 6-20: 6, >20: 8; largest 454
- ρ 0.5: 94 clusters; sizes 1: 45, 2-5: 23, 6-20: 18, >20: 8; largest 295
- ρ 0.7: 226 clusters; sizes 1: 121, 2-5: 77, 6-20: 20, >20: 8; largest 149

Largest clusters at ρ 0.3:

| cluster | size | family (basis) | session topics | strategies | best dev Sharpe |
|---|---|---|---|---|---|
| `exp-0105d095cb8e` | 454 | ewma_trend (seed) | trend 248, multi_timeframe 176, volatility_filter 11, seed 10 | plugin 444, template:ewma_trend 3, template:tsmom 3 | 1.08 |
| `exp-032b6bd1dc99` | 139 | vol_overlay (seed) | volatility_filter 138, seed 1 | plugin 138, template:vol_target 1 | 0.63 |
| `exp-07aaf1ad524b` | 51 | unmapped | multi_timeframe 50, trend 1 | plugin 51 | -5.16 |
| `exp-041b087bfdc1` | 38 | vol_overlay (tags) | volatility_filter 21, multi_timeframe 12, mean_reversion 5 | plugin 38 | 0.68 |
| `exp-1271b34fb3f3` | 28 | short_term_reversal (tags) | mean_reversion 28 | plugin 28 | 1.24 |
| `exp-00b167b3b6a1` | 26 | short_term_reversal (tags) | mean_reversion 26 | plugin 26 | -3.04 |
| `exp-0d5262b28e5c` | 23 | short_term_reversal (tags) | mean_reversion 23 | plugin 23 | 0.26 |
| `exp-0af714075f44` | 22 | short_term_reversal (tags) | mean_reversion 22 | plugin 22 | -1.41 |
| `exp-00afea057920` | 18 | short_term_reversal (tags) | mean_reversion 18 | plugin 18 | 0.51 |
| `exp-067ad7da1edb` | 14 | short_term_reversal (tags) | mean_reversion 14 | plugin 14 | 0.95 |
| `exp-14fc42b4e953` | 12 | short_term_reversal (tags) | mean_reversion 12 | plugin 12 | -3.65 |
| `exp-16cc7cb1cc27` | 9 | short_term_reversal (tags) | mean_reversion 9 | plugin 9 | -0.03 |
| `exp-2d4b6bded0c0` | 8 | unmapped | trend 8 | plugin 8 | -2.39 |
| `exp-0ea821e4a733` | 7 | short_term_reversal (tags) | mean_reversion 7 | plugin 7 | 1.30 |
| `exp-6f66f9f8715d` | 4 | short_term_reversal (tags) | mean_reversion 4 | plugin 4 | 0.43 |

Trials by family at ρ 0.3: ewma_trend 454, short_term_reversal 182, tsmom 2, unmapped 66, vol_overlay 184.

Seed placement:

| seed | dev Sharpe | split DSR | cluster ρ 0.3 (size) | cluster ρ 0.7 (size) |
|---|---|---|---|---|
| btc-brock-1-200 | 0.69 | 0.063 | `exp-0105d095cb8e` (454) | `exp-0105d095cb8e` (36) |
| btc-ewma-16-48 | 0.71 | 0.068 | `exp-0105d095cb8e` (454) | `exp-191a02405ccf` (3) |
| btc-ewma-32-96 | 0.36 | 0.016 | `exp-0105d095cb8e` (454) | `exp-191a02405ccf` (3) |
| btc-ewma-8-24 | 0.30 | 0.012 | `exp-0105d095cb8e` (454) | `exp-191a02405ccf` (3) |
| btc-faber-10m | 0.61 | 0.047 | `exp-0105d095cb8e` (454) | `exp-6b90b812ae50` (2) |
| btc-tsmom-12m | 1.01 | 0.175 | `exp-0105d095cb8e` (454) | `exp-95294e1fbe03` (1) |
| btc-tsmom-1m | 0.38 | 0.017 | `exp-0105d095cb8e` (454) | `exp-6a1df7681268` (1) |
| btc-tsmom-1w | 0.11 | 0.005 | `exp-6bf226feef2e` (2) | `exp-6bf226feef2e` (1) |
| btc-tsmom-3m | 0.40 | 0.019 | `exp-0105d095cb8e` (454) | `exp-f820c69541fb` (1) |
| btc-turtle-s1 | 0.20 | 0.007 | `exp-0105d095cb8e` (454) | `exp-012f341d77b4` (6) |
| btc-turtle-s2 | 0.27 | 0.010 | `exp-0105d095cb8e` (454) | `exp-01d04bc6f3f0` (34) |
| btc-voltarget-1m | 0.47 | 0.026 | `exp-032b6bd1dc99` (139) | `exp-16905e71f137` (10) |

New-family proposals (2; clusters of at least 5 trials that no seed or family majority claims):

- `prop-split-1016c7c9873a-exp-07aaf1ad524b-51`: 51 trials; topics {"topic:multi_timeframe": 50, "topic:trend": 1}; median dev Sharpe -5.77; representative `exp-771fc75b485b` (plugin:ccf6f732042ca6481abb8d3c5fc5ea38e32aaa91625882e724a5ac6d36cd6a0c, Sharpe -5.16)
- `prop-split-1016c7c9873a-exp-2d4b6bded0c0-8`: 8 trials; topics {"topic:trend": 8}; median dev Sharpe -3.93; representative `exp-d301f13cc5dc` (plugin:82227576f09880efb5c53d610e09208a095f9b39d86931f583e912bce8baed84, Sharpe -2.39)

Top 10 non-flat trials by dev Sharpe (daily), split DSR against the legacy DSR:

| experiment | kind | source | dev Sharpe | split DSR (K = 232) | legacy DSR (hourly) |
|---|---|---|---|---|---|
| `exp-640f008ef736` | backtest | topic:mean_reversion | 1.30 | 0.351 | 0.000 |
| `exp-244f749601d7` | backtest | topic:mean_reversion | 1.24 | 0.304 | 0.000 |
| `exp-fa6032c36556` | backtest | topic:mean_reversion | 1.13 | 0.240 | 0.000 |
| `exp-146cc5cdadb2` | backtest | topic:mean_reversion | 1.11 | 0.227 | 0.000 |
| `exp-3e3757ed910c` | backtest | topic:trend | 1.08 | 0.210 | 0.000 |
| `exp-e31599cb05f0` | backtest | topic:mean_reversion | 1.07 | 0.204 | 0.000 |
| `exp-12f40dd7bb6c` | backtest | topic:trend | 1.04 | 0.190 | 0.000 |
| `exp-2696248f7fd5` | backtest | topic:trend | 1.04 | 0.190 | 0.000 |
| `exp-f7ec47a71690` | backtest | topic:trend | 1.04 | 0.190 | 0.000 |
| `exp-2a9816c124d6` | backtest | topic:trend | 1.03 | 0.185 | 0.000 |

## ETH-USD (`split-c47a0be57a24`)

Trials: 913 (911 with returns, 2 without; 1 flat, 34 passive). Clustering input `12e87074f65ec881`.

| ρ threshold | 0.3 | 0.5 | 0.7 |
|---|---|---|---|
| K (winsorised, used) | 41 | 99 | **241** |
| K (not winsorised) | 39 | 94 | 229 |
| 2026-10-13 feasibility run, raw (winsorised) | 34 (38) | 108 (110) | 265 (276) |

K counts every cluster once, plus one for the flat cluster, one for the passive cluster and one per trial without returns (2). With K = 241 on T = 1267 days: SR0(K) = 1.52, σ0 = 0.54, so a split DSR of 0.9 needs an annualised dev Sharpe of at least 2.21 (more if fat tails widen σ_SR).

Cluster sizes (trials with returns, excluding flat and passive):

- ρ 0.3: 37 clusters; sizes 1: 13, 2-5: 11, 6-20: 5, >20: 8; largest 424
- ρ 0.5: 95 clusters; sizes 1: 43, 2-5: 31, 6-20: 14, >20: 7; largest 282
- ρ 0.7: 237 clusters; sizes 1: 139, 2-5: 77, 6-20: 14, >20: 7; largest 185

Largest clusters at ρ 0.3:

| cluster | size | family (basis) | session topics | strategies | best dev Sharpe |
|---|---|---|---|---|---|
| `exp-0055d5485f98` | 424 | channel_breakout (seed) | trend 222, multi_timeframe 183, volatility_filter 15, seed 3 | plugin 421, template:breakout 2, template:tsmom 1 | 1.22 |
| `exp-01aa1b30d06c` | 143 | vol_overlay (seed) | volatility_filter 141, multi_timeframe 1, seed 1 | plugin 142, template:vol_target 1 | 0.91 |
| `exp-043042614192` | 55 | ewma_trend (seed) | trend 21, multi_timeframe 14, volatility_filter 13, seed 7 | plugin 48, template:ewma_trend 3, template:ma_filter 2 | 1.42 |
| `exp-04ee69c71181` | 43 | short_term_reversal (tags) | mean_reversion 43 | plugin 43 | 0.35 |
| `exp-19885780619b` | 36 | unmapped | multi_timeframe 35, trend 1 | plugin 36 | -3.79 |
| `exp-0b23875cc6b0` | 33 | short_term_reversal (tags) | mean_reversion 33 | plugin 33 | -1.45 |
| `exp-0b46eb5312c9` | 27 | short_term_reversal (tags) | mean_reversion 26, volatility_filter 1 | plugin 27 | 0.99 |
| `exp-0d27d132d2c3` | 21 | short_term_reversal (tags) | mean_reversion 21 | plugin 21 | -2.17 |
| `exp-02719ff20c91` | 16 | short_term_reversal (tags) | mean_reversion 16 | plugin 16 | 0.27 |
| `exp-097a4b74c8fa` | 10 | short_term_reversal (tags) | mean_reversion 10 | plugin 10 | -0.81 |
| `exp-0c5540ace29c` | 8 | short_term_reversal (tags) | mean_reversion 8 | plugin 8 | -2.78 |
| `exp-3acddddf847c` | 8 | short_term_reversal (tags) | mean_reversion 8 | plugin 8 | -0.38 |
| `exp-038239082acd` | 7 | vol_overlay (tags) | volatility_filter 7 | plugin 7 | 0.15 |
| `exp-066912eda7a7` | 4 | vol_overlay (tags) | volatility_filter 4 | plugin 4 | 0.95 |
| `exp-0c9eef6bcf1f` | 4 | tsmom (seed) | volatility_filter 3, seed 1 | plugin 3, template:tsmom 1 | 0.63 |

Trials by family at ρ 0.3: channel_breakout 424, ewma_trend 55, short_term_reversal 198, tsmom 4, unmapped 36, vol_overlay 159.

Seed placement:

| seed | dev Sharpe | split DSR | cluster ρ 0.3 (size) | cluster ρ 0.7 (size) |
|---|---|---|---|---|
| eth-brock-1-200 | 0.97 | 0.154 | `exp-043042614192` (55) | `exp-043042614192` (11) |
| eth-ewma-16-48 | 0.38 | 0.017 | `exp-043042614192` (55) | `exp-3fba2953bf88` (6) |
| eth-ewma-32-96 | 0.41 | 0.019 | `exp-043042614192` (55) | `exp-3fba2953bf88` (6) |
| eth-ewma-8-24 | -0.17 | 0.001 | `exp-043042614192` (55) | `exp-525ec82c87e4` (1) |
| eth-faber-10m | 0.19 | 0.007 | `exp-043042614192` (55) | `exp-9f76fefe1dfd` (1) |
| eth-tsmom-12m | 0.67 | 0.056 | `exp-043042614192` (55) | `exp-5760e59dee86` (1) |
| eth-tsmom-1m | 0.51 | 0.030 | `exp-043042614192` (55) | `exp-f8678d9b1ac0` (1) |
| eth-tsmom-1w | 0.89 | 0.123 | `exp-0055d5485f98` (424) | `exp-3e8462eaaca4` (1) |
| eth-tsmom-3m | 0.35 | 0.015 | `exp-0c9eef6bcf1f` (4) | `exp-48b207e8494a` (1) |
| eth-turtle-s1 | 0.77 | 0.083 | `exp-0055d5485f98` (424) | `exp-0055d5485f98` (185) |
| eth-turtle-s2 | 0.31 | 0.013 | `exp-0055d5485f98` (424) | `exp-3037cdfe9481` (2) |
| eth-voltarget-1m | 0.73 | 0.071 | `exp-01aa1b30d06c` (143) | `exp-01aa1b30d06c` (8) |

New-family proposals (1; clusters of at least 5 trials that no seed or family majority claims):

- `prop-split-c47a0be57a24-exp-19885780619b-36`: 36 trials; topics {"topic:multi_timeframe": 35, "topic:trend": 1}; median dev Sharpe -4.08; representative `exp-f15bbff0cfa6` (plugin:f43e2308feb48fd613c82b03a9609ea0c9fe58b8a87cd07407c4d3f28c32aee3, Sharpe -3.79)

Top 10 non-flat trials by dev Sharpe (daily), split DSR against the legacy DSR:

| experiment | kind | source | dev Sharpe | split DSR (K = 241) | legacy DSR (hourly) |
|---|---|---|---|---|---|
| `exp-f323301a878f` | backtest | topic:volatility_filter | 1.42 | 0.430 | 0.000 |
| `exp-c198a5859083` | backtest | topic:volatility_filter | 1.36 | 0.383 | 0.000 |
| `exp-9de6760a5284` | backtest | topic:volatility_filter | 1.33 | 0.361 | 0.000 |
| `exp-cf035ce83432` | backtest | topic:volatility_filter | 1.33 | 0.361 | 0.000 |
| `exp-ab5f35e4874a` | backtest | topic:trend | 1.22 | 0.292 | 0.000 |
| `exp-1eb0686adfd0` | backtest | topic:trend | 1.22 | 0.289 | 0.000 |
| `exp-7dac4933334b` | backtest | topic:volatility_filter | 1.18 | 0.266 | 0.000 |
| `exp-86cd7fad3373` | backtest | topic:trend | 1.08 | 0.207 | 0.000 |
| `exp-48c793440fb1` | backtest | topic:trend | 1.05 | 0.191 | 0.000 |
| `exp-9757bcb81b5c` | backtest | topic:trend | 1.04 | 0.189 | 0.000 |

## Session conclusions

182 imported; citation check failed for 0.

| topic | decision | sessions |
|---|---|---|
| mean_reversion | reject | 46 |
| multi_timeframe | reject | 44 |
| trend | reject | 45 |
| trend | revise | 1 |
| volatility_filter | insufficient_evidence | 1 |
| volatility_filter | reject | 45 |
