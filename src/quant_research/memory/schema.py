"""Research memory tables in `experiments.db`. `family_conclusions` and `proposal_decisions` are append-only."""

from __future__ import annotations

from quant_research.experiments.store import ExperimentStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS return_index (
    experiment_id TEXT PRIMARY KEY, split_id TEXT NOT NULL, kind TEXT, session_id TEXT, family_tag TEXT,
    tag_source TEXT, n_days INTEGER, sharpe REAL, turnover REAL, no_returns INTEGER NOT NULL, flat INTEGER,
    passive INTEGER, added_at REAL);
CREATE INDEX IF NOT EXISTS ix_return_index_split ON return_index(split_id);
CREATE TABLE IF NOT EXISTS clusterings (
    id TEXT PRIMARY KEY, split_id TEXT NOT NULL, computed_at REAL, input_hash TEXT, n_trials INTEGER,
    k TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS cluster_members (
    clustering_id TEXT, threshold REAL, experiment_id TEXT, label TEXT,
    PRIMARY KEY (clustering_id, threshold, experiment_id));
CREATE TABLE IF NOT EXISTS family_proposals (
    id TEXT PRIMARY KEY, clustering_id TEXT, split_id TEXT, label TEXT, body TEXT, status TEXT, decided_at REAL);
CREATE TABLE IF NOT EXISTS proposal_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
    decided_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS family_conclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, family_id TEXT, product TEXT, source TEXT, session_id TEXT UNIQUE,
    body TEXT NOT NULL, citations_ok INTEGER, problems TEXT, created_at REAL);
"""


def ensure(store: ExperimentStore) -> ExperimentStore:
    store.ensure_schema(SCHEMA)
    return store
