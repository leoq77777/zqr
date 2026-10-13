"""SQLite experiment ledger. One `ExperimentStore` per process is the single writer;
parallel workers return results to it instead of writing directly."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,              -- running | completed | rejected | failed | aborted
    kind TEXT NOT NULL,                -- backtest | robustness | walkforward | holdout | grid
    session_id TEXT,
    parent_id TEXT,
    strategy_ref TEXT NOT NULL,
    strategy_hash TEXT,
    candidate_hash TEXT,
    parameters TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    data_hash TEXT,
    split_id TEXT,
    segment TEXT,
    window_start INTEGER,
    window_end INTEGER,
    engine_config_id TEXT,
    engine_config TEXT,
    engine_version TEXT,
    metrics TEXT,
    benchmarks TEXT,
    checks TEXT,
    statistics TEXT,
    diagnosis TEXT,
    failure_reason TEXT,
    result_dir TEXT,
    llm_suggestion TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_exp_split ON experiments(split_id);
CREATE INDEX IF NOT EXISTS ix_exp_session ON experiments(session_id);
CREATE INDEX IF NOT EXISTS ix_exp_candidate ON experiments(candidate_hash);
CREATE TABLE IF NOT EXISTS grids (
    id TEXT PRIMARY KEY, created_at REAL, session_id TEXT, strategy_ref TEXT, grid TEXT, n_candidates INTEGER
);
CREATE TABLE IF NOT EXISTS frozen_candidates (
    candidate_hash TEXT PRIMARY KEY, strategy_ref TEXT, parameters TEXT, frozen_at REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS holdout_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, split_id TEXT, candidate_hash TEXT, experiment_id TEXT,
    run_at REAL, prior_runs_on_split INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, created_at REAL, updated_at REAL, question TEXT, dataset_id TEXT, split_id TEXT,
    state TEXT, budget TEXT, usage TEXT, config TEXT, stop_reason TEXT, workspace TEXT
);
CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, at REAL, event TEXT, data TEXT
);
"""

JSON_FIELDS = {"parameters", "engine_config", "metrics", "benchmarks", "checks", "statistics", "diagnosis",
               "llm_suggestion", "grid", "budget", "usage", "config", "data", "policy"}
MIGRATIONS = {"experiments": {"statistics": "TEXT", "diagnosis": "TEXT", "policy_id": "TEXT", "policy": "TEXT"}}
# `blocked` = refused by the gatekeeper before any computation; not a trial and not budget usage.
NOT_A_TRIAL = ("blocked",)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class ExperimentStore:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            existing = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    def close(self) -> None:
        self._conn.close()

    # -- helpers ---------------------------------------------------------------------------------
    def _exec(self, sql: str, args: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, args)

    def _insert(self, table: str, row: dict[str, Any]) -> None:
        cols = list(row)
        vals = [json.dumps(v, default=str) if k in JSON_FIELDS and v is not None else v for k, v in row.items()]
        self._exec(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)

    def _update(self, table: str, key: str, key_value: Any, fields: dict[str, Any]) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v, default=str) if k in JSON_FIELDS and v is not None else v for k, v in fields.items()]
        self._exec(f"UPDATE {table} SET {sets} WHERE {key}=?", [*vals, key_value])

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        for k in JSON_FIELDS & out.keys():
            if out[k] is not None:
                out[k] = json.loads(out[k])
        return out

    # -- experiments -----------------------------------------------------------------------------
    def start_experiment(self, **fields: Any) -> str:
        exp_id = fields.pop("id", None) or new_id("exp")
        self._insert("experiments", {"id": exp_id, "created_at": time.time(), "status": "running", **fields})
        return exp_id

    def finish_experiment(self, exp_id: str, status: str, **fields: Any) -> None:
        self._update("experiments", "id", exp_id, {"status": status, "finished_at": time.time(), **fields})

    def set_notes(self, exp_id: str, notes: str) -> None:
        self._update("experiments", "id", exp_id, {"notes": notes})

    def get_experiment(self, exp_id: str) -> dict[str, Any] | None:
        return self._row(self._exec("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone())

    def list_experiments(self, limit: int = 50, **filters: Any) -> list[dict[str, Any]]:
        where = " AND ".join(f"{k}=?" for k in filters) or "1=1"
        rows = self._exec(f"SELECT * FROM experiments WHERE {where} ORDER BY created_at DESC LIMIT ?",
                          [*filters.values(), limit]).fetchall()
        return [self._row(r) for r in rows]

    def trial_count(self, split_id: str | None = None, session_id: str | None = None) -> int:
        """Cumulative attempts (all statuses, all sessions unless narrowed) used for multiple-testing context."""
        filters = {k: v for k, v in (("split_id", split_id), ("session_id", session_id)) if v}
        where = " AND ".join(f"{k}=?" for k in filters) or "1=1"
        return self._exec(f"SELECT COUNT(*) FROM experiments WHERE {where} AND kind != 'holdout' "
                          f"AND status NOT IN ({','.join('?' * len(NOT_A_TRIAL))})",
                          [*filters.values(), *NOT_A_TRIAL]).fetchone()[0]

    def session_usage_count(self, session_id: str) -> int:
        """Experiments that consume a session's experiment budget (everything except gate refusals)."""
        return self._exec(f"SELECT COUNT(*) FROM experiments WHERE session_id=? "
                          f"AND status NOT IN ({','.join('?' * len(NOT_A_TRIAL))})",
                          [session_id, *NOT_A_TRIAL]).fetchone()[0]

    def strategy_trial_count(self, strategy_hash: str, session_id: str | None, split_id: str) -> int:
        scope, value = ("session_id", session_id) if session_id else ("split_id", split_id)
        return self._exec(f"SELECT COUNT(*) FROM experiments WHERE strategy_hash=? AND {scope}=? "
                          f"AND kind != 'holdout' AND status NOT IN ({','.join('?' * len(NOT_A_TRIAL))})",
                          [strategy_hash, value, *NOT_A_TRIAL]).fetchone()[0]

    def find_completed(self, candidate_hash: str, data_hash: str, split_id: str, window: tuple[int, int],
                       engine_config_id: str, engine_version: str, policy_id: str) -> dict[str, Any] | None:
        row = self._exec(
            "SELECT * FROM experiments WHERE status='completed' AND candidate_hash=? AND data_hash=? AND split_id=? "
            "AND window_start=? AND window_end=? AND engine_config_id=? AND engine_version=? AND policy_id=? "
            "ORDER BY created_at LIMIT 1",
            (candidate_hash, data_hash, split_id, window[0], window[1], engine_config_id, engine_version,
             policy_id)).fetchone()
        return self._row(row)

    def research_sharpes(self, split_id: str) -> list[float]:
        """Annualised Sharpe of every completed dev/validation trial on a split (input to the DSR)."""
        rows = self._exec("SELECT metrics FROM experiments WHERE split_id=? AND status='completed' "
                          "AND kind != 'holdout' AND segment IN ('dev','validation')", (split_id,)).fetchall()
        return [json.loads(r[0])["sharpe"] for r in rows if r[0]]

    def mark_interrupted(self) -> int:
        """Experiments left `running` by a crashed process become `aborted`; returns count."""
        cur = self._exec("UPDATE experiments SET status='aborted', failure_reason='process exited while running',"
                         " finished_at=? WHERE status='running'", (time.time(),))
        return cur.rowcount

    def record_grid(self, strategy_ref: str, grid: dict, n_candidates: int, session_id: str | None = None) -> str:
        grid_id = new_id("grid")
        self._insert("grids", {"id": grid_id, "created_at": time.time(), "session_id": session_id,
                               "strategy_ref": strategy_ref, "grid": grid, "n_candidates": n_candidates})
        return grid_id

    # -- freezing / holdout ----------------------------------------------------------------------
    def freeze_candidate(self, candidate_hash: str, strategy_ref: str, parameters: dict, note: str = "") -> None:
        if self.is_frozen(candidate_hash):
            return
        self._insert("frozen_candidates", {"candidate_hash": candidate_hash, "strategy_ref": strategy_ref,
                                           "parameters": parameters, "frozen_at": time.time(), "note": note})

    def is_frozen(self, candidate_hash: str) -> bool:
        return self._exec("SELECT 1 FROM frozen_candidates WHERE candidate_hash=?",
                          (candidate_hash,)).fetchone() is not None

    def holdout_runs(self, split_id: str) -> list[dict[str, Any]]:
        rows = self._exec("SELECT * FROM holdout_runs WHERE split_id=? ORDER BY run_at", (split_id,)).fetchall()
        return [dict(r) for r in rows]

    def record_holdout_run(self, split_id: str, candidate_hash: str, experiment_id: str) -> int:
        prior = len(self.holdout_runs(split_id))
        self._insert("holdout_runs", {"split_id": split_id, "candidate_hash": candidate_hash,
                                      "experiment_id": experiment_id, "run_at": time.time(),
                                      "prior_runs_on_split": prior})
        return prior

    # -- sessions --------------------------------------------------------------------------------
    def create_session(self, **fields: Any) -> str:
        sid = fields.pop("id", None) or new_id("sess")
        now = time.time()
        self._insert("sessions", {"id": sid, "created_at": now, "updated_at": now, **fields})
        return sid

    def update_session(self, sid: str, **fields: Any) -> None:
        self._update("sessions", "id", sid, {"updated_at": time.time(), **fields})

    def get_session(self, sid: str) -> dict[str, Any] | None:
        return self._row(self._exec("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())

    def list_sessions(self) -> list[dict[str, Any]]:
        return [self._row(r) for r in self._exec("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()]

    def log_session_event(self, sid: str, event: str, data: dict | None = None) -> None:
        self._insert("session_events", {"session_id": sid, "at": time.time(), "event": event, "data": data})

    def session_events(self, sid: str) -> list[dict[str, Any]]:
        rows = self._exec("SELECT * FROM session_events WHERE session_id=? ORDER BY id", (sid,)).fetchall()
        return [self._row(r) for r in rows]
