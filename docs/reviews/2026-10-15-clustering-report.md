# Clustering report 2026-10-15

Rules: `docs/contracts-v2-appendix-a.md` (A1-A5, A7, A12). Residual correlations after removing the coin's daily return; average linkage; K(0.7) is the effective number of trials used by the split DSR.

## BTC-USD (`split-1016c7c9873a`)

Trials: 896 (891 with returns, 5 without; 3 flat, 37 passive). Clustering input `7e9eefcf23d6e22c`.

| ρ threshold | 0.3 | 0.5 | 0.7 |
|---|---|---|---|
| K (winsorised, used) | 42 | 100 | **231** |
| K (not winsorised) | 37 | 90 | 217 |
| 2026-10-13 feasibility run, raw (winsorised) | 34 (43) | 113 (116) | 255 (267) |

K counts every cluster once, plus one for the flat cluster, one for the passive cluster and one per trial without returns (5). With K = 231 on T = 1267 days: SR0(K) = 1.51, σ0 = 0.54, so a split DSR of 0.9 needs an annualised dev Sharpe of at least 2.20 (more if fat tails widen σ_SR).

Cluster sizes (trials with returns, excluding flat and passive):

- ρ 0.3: 35 clusters; sizes 1: 10, 2-5: 12, 6-20: 6, >20: 7; largest 454
- ρ 0.5: 93 clusters; sizes 1: 45, 2-5: 23, 6-20: 18, >20: 7; largest 295
- ρ 0.7: 224 clusters; sizes 1: 121, 2-5: 76, 6-20: 20, >20: 7; largest 149

Largest clusters at ρ 0.3:

| cluster | size | family (basis) | session topics | strategies | best dev Sharpe |
|---|---|---|---|---|---|
| `exp-0105d095cb8e` | 454 | ewma_trend + tsmom (seed, shared) | trend 248, multi_timeframe 176, volatility_filter 11, seed 10 | plugin 444, template:tsmom 3, template:ewma_trend 3 | 1.08 |
| `exp-032b6bd1dc99` | 139 | vol_overlay (seed) | volatility_filter 138, seed 1 | plugin 138, template:vol_target 1 | 0.63 |
| `exp-07aaf1ad524b` | 51 | unmapped | multi_timeframe 50, trend 1 | plugin 51 | -5.16 |
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
| `exp-0383f0283ce4` | 3 | short_term_reversal (tags) | mean_reversion 3 | plugin 3 | -1.19 |

Trials by family at ρ 0.3: ewma_trend 454, short_term_reversal 182, tsmom 456, unmapped 67, vol_overlay 146. A shared cluster counts under each of its families (454 trials in shared clusters); deduplicated, 784 trials are mapped to a family.

Seed placement:

| seed | dev Sharpe | split DSR | cluster ρ 0.3 (size) | cluster ρ 0.7 (size) |
|---|---|---|---|---|
| btc-brock-1-200 | 0.69 | 0.063 | `exp-0105d095cb8e` (454) | `exp-0105d095cb8e` (36) |
| btc-ewma-16-48 | 0.71 | 0.068 | `exp-0105d095cb8e` (454) | `exp-66c7207c1ac6` (3) |
| btc-ewma-32-96 | 0.36 | 0.016 | `exp-0105d095cb8e` (454) | `exp-66c7207c1ac6` (3) |
| btc-ewma-8-24 | 0.30 | 0.012 | `exp-0105d095cb8e` (454) | `exp-66c7207c1ac6` (3) |
| btc-faber-10m | 0.61 | 0.047 | `exp-0105d095cb8e` (454) | `exp-e2956351ab39` (2) |
| btc-tsmom-12m | 1.01 | 0.175 | `exp-0105d095cb8e` (454) | `exp-5ccb9b47156b` (1) |
| btc-tsmom-1m | 0.38 | 0.017 | `exp-0105d095cb8e` (454) | `exp-762132ec618d` (1) |
| btc-tsmom-1w | 0.11 | 0.005 | `exp-755005d7fcb8` (2) | `exp-755005d7fcb8` (1) |
| btc-tsmom-3m | 0.40 | 0.020 | `exp-0105d095cb8e` (454) | `exp-8f1d15305f28` (1) |
| btc-turtle-s1 | 0.20 | 0.007 | `exp-0105d095cb8e` (454) | `exp-012f341d77b4` (6) |
| btc-turtle-s2 | 0.27 | 0.011 | `exp-0105d095cb8e` (454) | `exp-01d04bc6f3f0` (34) |
| btc-voltarget-1m | 0.47 | 0.026 | `exp-032b6bd1dc99` (139) | `exp-16905e71f137` (10) |

New-family proposals (0; clusters of at least 5 trials that no seed or family majority claims):

- none

Rejected proposals (2; the clusters stay unmapped):

- `prop-split-1016c7c9873a-exp-07aaf1ad524b-51`: 51 trials; Human ruling 2026-10-15: rejected. Dev Sharpe medians -3.9 to -5.8; deterministic losses under friction from early agent high-frequency trading; no literature prior and no economic logic. Kept unmapped as negative-result knowledge; no family, no sleeve.
- `prop-split-1016c7c9873a-exp-2d4b6bded0c0-8`: 8 trials; Human ruling 2026-10-15: rejected. Dev Sharpe medians -3.9 to -5.8; deterministic losses under friction from early agent high-frequency trading; no literature prior and no economic logic. Kept unmapped as negative-result knowledge; no family, no sleeve.

Top 10 non-flat trials by dev Sharpe (daily), split DSR against the legacy DSR:

| experiment | kind | source | dev Sharpe | split DSR (K = 231) | legacy DSR (hourly) |
|---|---|---|---|---|---|
| `exp-640f008ef736` | backtest | topic:mean_reversion | 1.30 | 0.351 | 0.000 |
| `exp-244f749601d7` | backtest | topic:mean_reversion | 1.24 | 0.305 | 0.000 |
| `exp-fa6032c36556` | backtest | topic:mean_reversion | 1.13 | 0.240 | 0.000 |
| `exp-146cc5cdadb2` | backtest | topic:mean_reversion | 1.11 | 0.227 | 0.000 |
| `exp-3e3757ed910c` | backtest | topic:trend | 1.08 | 0.211 | 0.000 |
| `exp-e31599cb05f0` | backtest | topic:mean_reversion | 1.07 | 0.204 | 0.000 |
| `exp-12f40dd7bb6c` | backtest | topic:trend | 1.04 | 0.191 | 0.000 |
| `exp-2696248f7fd5` | backtest | topic:trend | 1.04 | 0.191 | 0.000 |
| `exp-f7ec47a71690` | backtest | topic:trend | 1.04 | 0.191 | 0.000 |
| `exp-2a9816c124d6` | backtest | topic:trend | 1.03 | 0.186 | 0.000 |

## ETH-USD (`split-c47a0be57a24`)

Trials: 913 (911 with returns, 2 without; 1 flat, 38 passive). Clustering input `992d8daa43f1a01d`.

| ρ threshold | 0.3 | 0.5 | 0.7 |
|---|---|---|---|
| K (winsorised, used) | 40 | 98 | **240** |
| K (not winsorised) | 38 | 93 | 228 |
| 2026-10-13 feasibility run, raw (winsorised) | 34 (38) | 108 (110) | 265 (276) |

K counts every cluster once, plus one for the flat cluster, one for the passive cluster and one per trial without returns (2). With K = 240 on T = 1267 days: SR0(K) = 1.52, σ0 = 0.54, so a split DSR of 0.9 needs an annualised dev Sharpe of at least 2.20 (more if fat tails widen σ_SR).

Cluster sizes (trials with returns, excluding flat and passive):

- ρ 0.3: 36 clusters; sizes 1: 13, 2-5: 10, 6-20: 5, >20: 8; largest 424
- ρ 0.5: 94 clusters; sizes 1: 43, 2-5: 30, 6-20: 14, >20: 7; largest 282
- ρ 0.7: 236 clusters; sizes 1: 139, 2-5: 76, 6-20: 14, >20: 7; largest 185

Largest clusters at ρ 0.3:

| cluster | size | family (basis) | session topics | strategies | best dev Sharpe |
|---|---|---|---|---|---|
| `exp-0055d5485f98` | 424 | channel_breakout (seed) | trend 222, multi_timeframe 183, volatility_filter 15, seed 3 | plugin 421, template:breakout 2, template:tsmom 1 | 1.22 |
| `exp-01aa1b30d06c` | 143 | vol_overlay (seed) | volatility_filter 141, multi_timeframe 1, seed 1 | plugin 142, template:vol_target 1 | 0.91 |
| `exp-0344c669d1fe` | 55 | ewma_trend (seed) | trend 21, multi_timeframe 14, volatility_filter 13, seed 7 | plugin 48, template:ewma_trend 3, template:tsmom 2 | 1.42 |
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
| `exp-0c9eef6bcf1f` | 4 | tsmom (seed) | volatility_filter 3, seed 1 | plugin 3, template:tsmom 1 | 0.63 |
| `exp-0f09e3d2f5c5` | 4 | short_term_reversal (tags) | mean_reversion 4 | plugin 4 | -6.66 |

Trials by family at ρ 0.3: channel_breakout 424, ewma_trend 55, short_term_reversal 198, tsmom 4, unmapped 36, vol_overlay 155. A shared cluster counts under each of its families (0 trials in shared clusters); deduplicated, 836 trials are mapped to a family.

Seed placement:

| seed | dev Sharpe | split DSR | cluster ρ 0.3 (size) | cluster ρ 0.7 (size) |
|---|---|---|---|---|
| eth-brock-1-200 | 0.97 | 0.154 | `exp-0344c669d1fe` (55) | `exp-2ac997141999` (11) |
| eth-ewma-16-48 | 0.38 | 0.017 | `exp-0344c669d1fe` (55) | `exp-0a2d4763c483` (6) |
| eth-ewma-32-96 | 0.41 | 0.019 | `exp-0344c669d1fe` (55) | `exp-0a2d4763c483` (6) |
| eth-ewma-8-24 | -0.17 | 0.001 | `exp-0344c669d1fe` (55) | `exp-1b7a4912be1a` (1) |
| eth-faber-10m | 0.19 | 0.007 | `exp-0344c669d1fe` (55) | `exp-2b90ad4ad720` (1) |
| eth-tsmom-12m | 0.67 | 0.056 | `exp-0344c669d1fe` (55) | `exp-628485b9d8c6` (1) |
| eth-tsmom-1m | 0.51 | 0.030 | `exp-0344c669d1fe` (55) | `exp-0344c669d1fe` (1) |
| eth-tsmom-1w | 0.89 | 0.123 | `exp-0055d5485f98` (424) | `exp-4b27ed1782ca` (1) |
| eth-tsmom-3m | 0.35 | 0.015 | `exp-0c9eef6bcf1f` (4) | `exp-48ccf824076a` (1) |
| eth-turtle-s1 | 0.77 | 0.083 | `exp-0055d5485f98` (424) | `exp-0055d5485f98` (185) |
| eth-turtle-s2 | 0.31 | 0.013 | `exp-0055d5485f98` (424) | `exp-3037cdfe9481` (2) |
| eth-voltarget-1m | 0.73 | 0.072 | `exp-01aa1b30d06c` (143) | `exp-01aa1b30d06c` (8) |

New-family proposals (0; clusters of at least 5 trials that no seed or family majority claims):

- none

Rejected proposals (1; the clusters stay unmapped):

- `prop-split-c47a0be57a24-exp-19885780619b-36`: 36 trials; Human ruling 2026-10-15: rejected. Dev Sharpe medians -3.9 to -5.8; deterministic losses under friction from early agent high-frequency trading; no literature prior and no economic logic. Kept unmapped as negative-result knowledge; no family, no sleeve.

Top 10 non-flat trials by dev Sharpe (daily), split DSR against the legacy DSR:

| experiment | kind | source | dev Sharpe | split DSR (K = 240) | legacy DSR (hourly) |
|---|---|---|---|---|---|
| `exp-f323301a878f` | backtest | topic:volatility_filter | 1.42 | 0.430 | 0.000 |
| `exp-c198a5859083` | backtest | topic:volatility_filter | 1.36 | 0.384 | 0.000 |
| `exp-9de6760a5284` | backtest | topic:volatility_filter | 1.33 | 0.361 | 0.000 |
| `exp-cf035ce83432` | backtest | topic:volatility_filter | 1.33 | 0.361 | 0.000 |
| `exp-ab5f35e4874a` | backtest | topic:trend | 1.22 | 0.293 | 0.000 |
| `exp-1eb0686adfd0` | backtest | topic:trend | 1.22 | 0.290 | 0.000 |
| `exp-7dac4933334b` | backtest | topic:volatility_filter | 1.18 | 0.267 | 0.000 |
| `exp-86cd7fad3373` | backtest | topic:trend | 1.08 | 0.208 | 0.000 |
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
