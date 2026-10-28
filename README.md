# ZQR

Personal systematic research desk for Coinbase **BTC-USD** and **ETH-USD** spot: long or cash, hourly bars.

An isolated coding agent proposes strategies. Deterministic code owns everything that changes state: backtests, Gate 1 / Gate 2, promotion, paper risk, and accounting. A model’s opinion never moves a candidate. **Live trading is out of scope**; there are no exchange trading credentials in this system.

Paper fills are simulated on public Coinbase candles (next-bar open, fee, slippage). Research uses the deployable cost profile `coinbase-lf-allin-v1` (0.20% + 2 bps).

## Layout

| Path | What it is |
|---|---|
| `src/quant_research/` | Desk, campaigns, gates, paper fleet, gateway |
| `docs/development.md` | As-built behaviour, commands, deployment |
| `docs/contracts-v2.md` | Frozen interfaces |
| `docs/development-plan-v2.md` | Design intent (historical; the code wins if they disagree) |
| `docs/reviews/` | Human-only run notes |

Ship with `zqr release` (Docker images on a separate host). Day-to-day: `zqr status`, `zqr desk panel`. Details are in `docs/development.md`.

## Status

The closed loop is deployed: supervisor, paper fleet, research sessions with a daily cost cap, standing authorization, and a local blotter. Gate 1+2 admits can go to official paper under a super authorization (`zqr desk authorize --renew --super`) instead of waiting for the next holdout split (earliest 2027-10-12).

Two weeks of desk time (2026-10-13–27) ran hundreds of sessions without fleet or supervisor faults. Most work still does not become a tradable paper book. See `docs/reviews/2026-10-27-desk-run-report.md`.

## Current problems

- **Idle research.** Family trial quotas on the current split are full. The generator queues nothing; the loop waits. Raising quota on the same trend sleeves would mostly re-test the same history.
- **Admits that cannot paper.** Paper runs `template:` members only. Most Gate 1+2 versions add a plugin (often `calendar_filter`) and stay off the roster. Several admits look like the same edge (repeated appraisal ~1.075).
- **Wasted sessions.** Many campaigns are `open_question` and finish on session 1, still billed as a design (pro) session. Others exhaust with zero sessions after the family quota is already gone.
- **Paper is not profit.** Gates say “not worse than the reference on this split,” not “green next week.” Official admitted books have only a few days of forward marks. There is no live P&L.

## Next

Keep using paper as the forward sample (super authorization), not a year-long holdout wait. Before restarting the loop: stop duplicate edges at Gate 1, reserve quota when queueing, and stop sending cheap follow-ups to the design model. Restart research with a few **non-trend** families rather than more quota on ewma / tsmom / calendar. Turn repeated plugin admits into templates if they should trade; do not dump correlated plugins onto the six paper slots.

Not planned: automated live trading.
