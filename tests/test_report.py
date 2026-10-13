import json
from datetime import UTC, datetime

from quant_research.desk import research as rl
from quant_research.desk.config import DeskConfig, save_config
from quant_research.desk.report import write_daily_report
from quant_research.experiments.store import ExperimentStore
from quant_research.paper.fleet import PaperFleet
from quant_research.paper.ledger import Ledger
from tests.conftest import make_mandate
from tests.test_fleet import HOUR, _entry, _setup


def test_daily_report_from_fixed_data(tmp_path, dataset):
    t, paths, roster, client = _setup(tmp_path, [_entry("btc-sma"), _entry("btc-bh", ref="template:buy_and_hold")])
    fleet = PaperFleet(paths, roster, client, clock=lambda: t["now"], seed_bars=1000)
    fleet.step()
    for _ in range(6):
        t["now"] += HOUR
        fleet.step()
    day = rl.utc_day(t["now"])

    store = ExperimentStore(paths.db)
    m = make_mandate(dataset)
    store.put_mandate(m.mandate_id(), m.model_dump(mode="json"))
    save_config(paths, DeskConfig(mandates={"BTC-USD": m.mandate_id()}, daily_cost_cap_usd=20))
    rl.ledger_path(paths).write_text("\n".join(json.dumps(r) for r in [
        {"run_id": "desk-a", "day": day, "product": "BTC-USD", "topic": "trend", "status": "ok",
         "failure_class": "none", "cost_usd": 0.21, "audit_ok": True, "verdict": "rejected",
         "candidate_id": "cand-1", "violations": 0, "promotion": None},
        {"run_id": "desk-b", "day": day, "product": "BTC-USD", "topic": "trend", "status": "failed",
         "failure_class": "environment.docker", "cost_usd": 0.0, "audit_ok": True, "violations": 1},
        {"run_id": "desk-old", "day": "2000-01-01", "failure_class": "none", "cost_usd": 9.0},
    ]) + "\n")

    text = write_daily_report(paths, store, day).read_text()
    assert f"# Desk report {day}" in text
    assert "| btc-sma | sma_cross | BTC-USD |" in text and "| btc-bh | buy_and_hold |" in text
    assert "0 parity mismatches" in text and "6/6 |" in text
    assert "Sessions: 2 (none 1, environment.docker 1)" in text and "0.21 USD" in text
    assert "| BTC-USD |" in text and "holdout viewings left" in text
    assert "desk-b: environment.docker" in text
    assert "desk-old" not in text

    ledger = Ledger(paths.paper / "btc-sma" / "paper.db")  # a tampered decision is caught by the parity check
    row = ledger._conn.execute("SELECT id, data FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    data = json.loads(row[1])
    data["target"]["target_fraction"] = 0.5
    ledger._conn.execute("UPDATE decisions SET data=? WHERE id=?", (json.dumps(data), row[0]))
    ledger.close()
    text = write_daily_report(paths, store, day).read_text()
    assert "1 parity mismatches" in text and "recorded 0.5" in text
    store.close()


def test_report_without_any_activity(tmp_path):
    from quant_research.paths import get_paths

    paths = get_paths(tmp_path / "empty")
    store = ExperimentStore(paths.db)
    day = datetime(2026, 10, 12, tzinfo=UTC).strftime("%Y-%m-%d")
    text = write_daily_report(paths, store, day).read_text()
    assert "no paper strategies" in text and "Sessions: 0" in text
    store.close()


def test_report_day_return_comes_from_marks(tmp_path):
    from quant_research.desk import promotion
    from quant_research.desk.report import paper_section
    from quant_research.paper.fleet import Roster, RosterEntry, save_roster
    from quant_research.paper.runner import run_replay
    from quant_research.paths import get_paths
    from quant_research.portfolio.policy import PortfolioPolicy
    from tests.conftest import T0
    from tests.test_paper import PRODUCT, _bars, _cfg

    paths = get_paths(tmp_path / "home")
    policy = PortfolioPolicy(rebalance_every=24)
    entry = RosterEntry(name="btc-sma", product="BTC-USD", strategy_ref="template:sma_cross",
                        parameters={"fast": 5, "slow": 20}, policy=policy)
    save_roster(promotion.roster_path(paths), Roster(fee_rate=0.001, slippage_bps=2, entries=[entry]))
    trader = run_replay(_cfg(name="btc-sma", policy=policy), _bars(72), paths.paper / "btc-sma", PRODUCT)
    marks = trader.ledger.marks()
    trader.ledger.close()
    lines, summary = paper_section(paths, "2024-01-02")
    start = marks[marks["at"] <= T0 + 24 * HOUR].iloc[-1]
    end = marks[marks["at"] <= T0 + 48 * HOUR].iloc[-1]
    assert summary["strategies"] == 1
    assert f"{end['equity'] / start['equity'] - 1:+.2%}" in "\n".join(lines)
