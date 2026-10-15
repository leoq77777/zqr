# Draft amendment A13.8: extreme prints stay in the data, execution is guarded (supersedes A13.5)

Date: 2026-10-15. Status: **draft for the human's approval; no code, no data change until approved.** Source: the
reassessment `docs/reviews/2026-10-15-reassessment/zqr-reassessment.md`, finding R5. When approved, the rule below
goes into `docs/contracts-v2-appendix-a.md` as A13.8, and A13.5 gets one line "Superseded by A13.8 (date)"; its
text stays as the record of what the current datasets were built with.

## 1. Why A13.5 has to change

A13.5 drops three pre-2021 hourly bars as prices "no order of any size could have traded at" and fills each with a
flat bar at the previous close:

| Bar (UTC) | Recorded | A13.5 check |
|---|---|---|
| BTC-USD 2017-04-15 23:00 | open and low 0.06, close 1,178.85, previous close 1,182.99 | isolated open and wick |
| ETH-USD 2017-04-15 23:00 | open and high 74.98, close 48.94, previous close 48.53 | isolated open and wick |
| ETH-USD 2017-06-21 19:00 | low 0.10, open 315.97, close 298.00 | isolated wick |

- The third bar is real. The exchange's own account (Adam White, then GDAX's head, [ETH-USD trading update
  3](https://medium.com/@AdamLWhite/eth-usd-trading-update-3-69e623335e00)) says stop orders and margin
  liquidations did trade ETH down to 0.10 and the trades were honoured. The statement in A13.5 is false for it.
- Dropping the bar rewrites more than the extreme price: the filled bar has close 315.97 instead of the real
  298.00, volume 0, and high = low, so channels, ATR and volatility read a bar that never existed.
- The two 2017-04-15 opens follow an exchange outage. Whether trades printed there is not verified either way.
  The harm A13.5 was written against is real, though: the engine fills at the next bar's open, so a strategy that
  bought at the BTC open shows a 20,000x return. That is an execution-model problem (no order of useful size fills
  at the first print after an outage), not a reason to rewrite the market record.

## 2. What the current rule touched

- All three bars are in the **dev** segment of the long-dev splits (the A13.4 children of `split-1016c7c9873a`
  and `split-c47a0be57a24`). A13.5 flags no bar from 2021-01-01 on, so validation, holdout, their viewings and
  closures are unaffected. The paper bar store does not use the rule.
- Research recorded on the long-dev splits: seed characterisation and clustering (K(0.7) BTC 10 + 231, ETH
  10 + 240), the return archive and family archives, the gate 1 references (`pv-bef1798ef4d9` BTC,
  `pv-77bad34a29c6` ETH), the campaigns and their trials, and the gate records (two `tsmom` hypotheses passed
  gate 1 and failed gate 2; none is admitted).
- The size of the effect is not measured. The first E1 rehearsal (raw data, before A13.5) showed an ETH ladder
  liquidation in 2017 that came from these prints; how much the cleaned bars move the current results is what the
  comparison in section 4, step 0, measures.

## 3. Proposed rule (A13.8)

**Data layers.**

- Raw: the fetched candle windows under `raw/` in the ZQR home, unchanged (as today).
- Bars: `validate_bars` keeps the structural checks of contracts v2 (positive, finite, OHLC-consistent, aligned,
  complete, deduplicated). It no longer drops a bar for its price level.
- Flags: the A13.5 detectors stay, with the same thresholds, but annotate instead of drop: a column `print_flag`
  (`""`, `isolated_open`, `isolated_wick`, or both) and the list in the validation report. The flags are part of
  the dataset, so they enter its hash.

**Verified errors.** A bar field is corrected only from an entry a human-only command records in the ZQR home
(`raw/print_corrections.json`): product, bar time, field (`open`, `high`, `low` or `close`), recorded and corrected value, a source showing the
venue busted or never had the trade, and the approval note. A correction replaces that field only, never the whole
bar or its volume. The corrections file's hash goes into the manifest of every dataset built with it. Without an
entry the bar stays as recorded.

**Execution guard (backtest engine).** At a bar whose open is flagged `isolated_open` and not corrected, fills
use the adverse reference already in the engine (`adverse_fill`): a buy at the higher of the open and the decision
close, a sell at the lower, and the equity that sizes the order uses the same price. Wicks never reach a fill (the
engine fills only at opens; the desk places no stop orders). Indicators read the bars as recorded, including the
0.10 low: strategies that use highs and lows see the real event.

**Reporting.** Every run whose window contains a flagged bar lists it in its summary. The run's metrics are not
adjusted.

**Scope.** Datasets frozen or extended after approval. The current datasets are immutable and stay as built; the
new versions in section 4 replace them for research.

**Decisions for the human.**

1. The 2017-04-15 opens: guard only (proposed), unless a source turns up that the venue busted those trades, in
   which case a correction entry replaces the open.
2. The guard applies only at flagged opens (proposed), not as a general adverse-fill setting.
3. Family quotas on the new split count the old split's trials too (proposed; otherwise a data revision would
   reset every family's quota).

## 4. New dataset versions (plan; runs only after approval)

0. **Sensitivity before deciding** (agent; offline on a copy of production, nothing written to production). Dev
   runs of every seed member and both reference portfolios on three versions of the long dev: (A) the current
   A13.5 data; (B) the A13.8 data (bars kept, flagged, guarded fills); (C) bars kept without the guard, to show
   what the guard prevents. Reported per product: Sharpe, return and drawdown with and without the ladder, ladder
   events, the signals of the channel and ATR strategies in the 30 days after each flagged bar, and K(0.7) if it
   changes. Written to `docs/reviews/<date>-a13-8-sensitivity.md` for the human. Dev only; validation and holdout
   are not read.
1. **Code** (agent, after approval): flags and the corrections file in `data/validate.py`; the guard in
   `backtest/engine.py`; the manifest fields; a lineage kind `revalidated` (same windows, different data) next to
   A13.4's backward extension; quotas and K counting the revalidated parent; tests: the flash-crash bar kept with
   its close and volume, a correction changes one field only, a guarded buy at a 0.06 open fills at the previous
   close, the flags change the dataset hash.
2. **Release**, with research paused on the host (`zqr research stop`).
3. **Data** (agent): `zqr extend-dataset` again from the same 2021+ base datasets and raw windows under the new
   rule (new dataset ids); `zqr create-split --like` the current long-dev splits.
4. **Lineage and mandates** (human): `zqr split-lineage` with kind `revalidated` from the current long-dev split.
   The new split inherits its closures, K and trials, so everything tried on the old data counts on the new. Then
   new mandates on the new splits and `zqr desk use-mandate`.
5. **Seeds and references** (agent): `zqr memory characterise`, `zqr memory run --sandbox docker`, seed
   portfolios, `zqr portfolio dev <vid> --reference`. The new seed runs count on the new split, as in A13.4.
6. **Campaigns** (human): a campaign's body names its split and never changes. Proposed: let a running campaign
   finish on the old split, cancel the queued ones and re-approve their replacements on the new split. Gate
   records stay on the old split (none admitted, so nothing moves to the library).
7. **Review** (agent): `docs/reviews/<date>-a13-8-first-look.md`, old against new long dev for the seeds and
   references, with the flagged bars marked.

## 5. What does not change

`docs/contracts-v2.md`; the validation and holdout windows, their uses, viewings and closures; the paper bar store
and paper fills; the A13.5 detection thresholds (they now flag instead of drop).
