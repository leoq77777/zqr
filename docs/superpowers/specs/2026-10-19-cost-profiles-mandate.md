# Cost profiles and mandate template fields (draft for implementers)

**Status:** policy approved by the human on 2026-10-19 (`docs/investment-policy.md` §6). Code
wiring shipped the same day: `quant_research.backtest.costs`, `Mandate.cost_profile` (optional for
legacy bodies), CLI `--cost-profile`, desk setup, roster/paper defaults, daily report column.

**Rules:** investment policy §6; contracts-v2 §4 (fill semantics unchanged); next-features D4
decisions in `docs/superpowers/plans/2026-10-19-next-features.md`.

---

## 1. Named profiles

| `cost_profile` | Role | `fee_rate` | `slippage_bps` | ≈ round trip |
|---|---|---|---|---|
| `coinbase-lf-allin-v1` | **Authoritative deployable baseline** | 0.0020 | 2.0 | 0.44% |
| `coinbase-retail-taker-v0` | Worst-case retail taker contrast / stress | 0.0060 | 5.0 | 1.30% |

`costs_x2` always doubles the mandate’s **base** `fee_rate` and `slippage_bps`.

---

## 2. Mandate fields (additive)

Proposed additions on `Mandate` (Pydantic, `extra=forbid`, frozen):

| Field | Type | Required | Notes |
|---|---|---|---|
| `cost_profile` | `Literal["coinbase-lf-allin-v1", "coinbase-retail-taker-v0"]` | yes (new mandates) | Named world; must match `engine_config.costs` numerics |
| `engine_config.costs.fee_rate` | float | yes | Must equal the profile table |
| `engine_config.costs.slippage_bps` | float | yes | Must equal the profile table |
| `engine_config.costs.source` | string | yes | Must use the canonical source text for that profile (below) |

**Validation rule (to implement):** if `cost_profile` is set, reject create when fee/slip disagree
with the table, or when `source` does not start with the profile’s canonical prefix
(`cost_profile:coinbase-lf-allin-v1:` or `cost_profile:coinbase-retail-taker-v0:`).

Until the field exists in code, put the same content in `notes` as
`cost_profile=coinbase-lf-allin-v1` and keep full honesty in `costs.source`.

---

## 3. Canonical `source` strings

### 3.1 `coinbase-lf-allin-v1`

```text
cost_profile:coinbase-lf-allin-v1: assumed all-in low-frequency rebalancing friction proxy for Coinbase Advanced Trade (BTC-USD/ETH-USD spot). fee_rate=0.0020 and slippage_bps=2.0 are a conservative pack of steady-state maker-biased economics after normal personal 30-day volume clears the lowest published tier, occasional taker cleanup, tight spot spreads, and a ~20-30% buffer. The backtest engine still uses next-bar-open market-style fills with certain execution (contracts-v2 §4); this is NOT a maker queue or partial-fill simulator. Verified schedule context: Coinbase Advanced Trade public tiers as of 2026-10-15 (taker 0.60%/maker 0.40% below $10k 30d; maker falls with volume). Policy: docs/investment-policy.md §6 (amended 2026-10-19).
```

### 3.2 `coinbase-retail-taker-v0`

```text
cost_profile:coinbase-retail-taker-v0: worst-case retail taker reference. fee_rate=0.0060 (Coinbase Advanced Trade lowest public taker tier) plus slippage_bps=5.0. Matches the 2026-10-13/2026-10-15 sole baseline; retained for stress/contrast only. Engine: next-bar-open market-style fills (contracts-v2 §4). Not the deployable admission world unless this profile is the mandate base. Policy: docs/investment-policy.md §6.
```

---

## 4. Example mandate JSON (deployable baseline)

Humans copy, set `dataset_id` / `split_id` / `question`, review every threshold, then
`zqr mandate create --file ...`.

```json
{
  "question": "Under coinbase-lf-allin-v1 costs, is there a long-or-cash rule on BTC-USD hourly bars that survives costs_x2, a one-bar delay with adverse fills, and the split DSR?",
  "dataset_id": "REPLACE_DATASET_ID",
  "split_id": "REPLACE_SPLIT_ID",
  "cost_profile": "coinbase-lf-allin-v1",
  "engine_config": {
    "initial_cash": 10000.0,
    "costs": {
      "fee_rate": 0.002,
      "slippage_bps": 2.0,
      "source": "cost_profile:coinbase-lf-allin-v1: assumed all-in low-frequency rebalancing friction proxy for Coinbase Advanced Trade (BTC-USD/ETH-USD spot). fee_rate=0.0020 and slippage_bps=2.0 are a conservative pack of steady-state maker-biased economics after normal personal 30-day volume clears the lowest published tier, occasional taker cleanup, tight spot spreads, and a ~20-30% buffer. The backtest engine still uses next-bar-open market-style fills with certain execution (contracts-v2 §4); this is NOT a maker queue or partial-fill simulator. Verified schedule context: Coinbase Advanced Trade public tiers as of 2026-10-15 (taker 0.60%/maker 0.40% below $10k 30d; maker falls with volume). Policy: docs/investment-policy.md §6 (amended 2026-10-19)."
    },
    "min_trade_notional": 1.0,
    "rebalance_tolerance": 0.0,
    "execution_delay_bars": 0,
    "adverse_fill": false,
    "base_increment": null
  },
  "policy": {},
  "evaluation_design": {"kind": "single_validation"},
  "primary_metric": "sharpe",
  "acceptance": {
    "min_primary": 1.0,
    "min_dsr": 0.9,
    "dsr_waiver": null,
    "min_round_trips": 10,
    "max_drawdown": -0.25,
    "require_costs_x2": true,
    "require_delay": true,
    "require_beats_policy_matched": false,
    "require_bootstrap_lower_positive": false
  },
  "budget": {
    "max_rounds": 1,
    "max_candidates": 1,
    "max_experiments": 12,
    "max_wall_seconds": 1800,
    "max_trials_per_strategy": null
  },
  "holdout": {
    "max_viewings": 10,
    "min_primary": 0.5,
    "min_total_return": 0.0
  },
  "agent_segments": ["dev"],
  "notes": "cost_profile=coinbase-lf-allin-v1 (authoritative). Do not mix coinbase-retail-taker-v0 trials into this mandate's DSR. costs_x2 doubles this baseline (~0.88% RT)."
}
```

Acceptance numbers above mirror current desk defaults (`docs/development.md`); edit per campaign
before create. **Immutability:** after `mandate create`, change costs only by creating a new
mandate (new id).

---

## 5. Example: stress mandate base (`coinbase-retail-taker-v0`)

Use only when the research question is explicitly about surviving worst-case retail taking.
Otherwise prefer lf-allin as base and run taker-v0 as a labelled sensitivity after freeze.

```json
{
  "cost_profile": "coinbase-retail-taker-v0",
  "engine_config": {
    "costs": {
      "fee_rate": 0.006,
      "slippage_bps": 5.0,
      "source": "cost_profile:coinbase-retail-taker-v0: worst-case retail taker reference. fee_rate=0.0060 (Coinbase Advanced Trade lowest public taker tier) plus slippage_bps=5.0. Matches the 2026-10-13/2026-10-15 sole baseline; retained for stress/contrast only. Engine: next-bar-open market-style fills (contracts-v2 §4). Not the deployable admission world unless this profile is the mandate base. Policy: docs/investment-policy.md §6."
    }
  }
}
```

---

## 6. Implementation checklist

- [x] Add `cost_profile` to `research/mandate.py` with profile→costs table and validator
- [x] `quant_research.backtest.costs` module exporting both profiles + source text
- [x] CLI: `zqr mandate template` defaults to lf-allin-v1; `--cost-profile` flag
- [x] Desk setup: new desk mandates use lf-allin-v1; do not rewrite immutable old mandates
- [x] Paper roster / PaperConfig / SimulatedBroker defaults → lf-allin numerics (0.002 / 2 bps)
- [x] Golden G1–G3 pinned to `coinbase-retail-taker-v0` (hand-worked 0.6% schedule)
- [x] Daily report: cost profile column (legacy mandates show `(legacy)`)
- [x] `mandate_id` omits `cost_profile` when null so legacy hashes stay stable

---

## 7. Explicit non-goals

- No maker/limit order simulator in this change
- No volume-tier schedule inside the backtest
- No silent edit of historical experiment rows or old mandate bodies
- No dropping `require_costs_x2`
