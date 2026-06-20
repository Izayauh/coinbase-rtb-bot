"""
SQLite schema + explicit migrations for the research store (schema_v1).

Migrations are an ordered list of (version, sql). `apply_migrations` runs every
migration whose version is greater than the recorded user_version, inside one
transaction per migration, and bumps PRAGMA user_version. This is deterministic,
forward-only, and independent of bot/db.py.

raw_events is append-only and enforced at the DB level by BEFORE UPDATE/DELETE
triggers that RAISE(ABORT). That guarantee is verified by tests/test_storage.py.
"""
from __future__ import annotations

import sqlite3
from typing import List, Tuple

SCHEMA_VERSION = 4

# --- v1 -------------------------------------------------------------------
_V1 = """
-- Source registry: one row per logical data source.
CREATE TABLE sources (
    source_id      TEXT PRIMARY KEY,
    source_kind    TEXT NOT NULL,          -- coinbase_ws | fred | bls | edgar | cftc | funding | onchain
    endpoint       TEXT NOT NULL,          -- URL or wss://... + channel
    schema_version INTEGER NOT NULL,
    first_seen_us  INTEGER NOT NULL,
    notes          TEXT
);

-- Ingestion-run ledger: one row per collector invocation.
CREATE TABLE ingestion_runs (
    run_id      TEXT PRIMARY KEY,
    collector   TEXT NOT NULL,
    params_json TEXT NOT NULL,
    started_us  INTEGER NOT NULL,
    ended_us    INTEGER,
    status      TEXT NOT NULL,             -- RUNNING | OK | ERROR | STORAGE_BLOCKED
    raw_count   INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

-- Append-only raw evidence. Deduplicated on payload_sha256.
CREATE TABLE raw_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,
    source_id        TEXT NOT NULL,
    channel          TEXT NOT NULL,
    payload_sha256   TEXT NOT NULL UNIQUE,
    payload          TEXT NOT NULL,        -- canonical JSON text
    event_time_us    INTEGER,
    recv_time_us     INTEGER NOT NULL,
    ingest_time_us   INTEGER NOT NULL,
    sequence_num     INTEGER,
    connection_epoch INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_raw_channel_time ON raw_events(channel, recv_time_us);

-- Append-only enforcement (DB-level backstop).
CREATE TRIGGER raw_events_no_update BEFORE UPDATE ON raw_events
BEGIN SELECT RAISE(ABORT, 'raw_events is append-only'); END;
CREATE TRIGGER raw_events_no_delete BEFORE DELETE ON raw_events
BEGIN SELECT RAISE(ABORT, 'raw_events is append-only'); END;

-- Normalized market trades.
CREATE TABLE trades (
    product_id    TEXT NOT NULL,
    trade_id      TEXT NOT NULL,
    price         REAL NOT NULL,
    size          REAL NOT NULL,
    side          TEXT NOT NULL,           -- BUY | SELL (Coinbase maker side)
    event_time_us INTEGER NOT NULL,
    recv_time_us  INTEGER NOT NULL,
    raw_id        INTEGER NOT NULL,
    PRIMARY KEY (product_id, trade_id)
);
CREATE INDEX idx_trades_time ON trades(product_id, event_time_us);

-- Normalized Level 2 updates (new_quantity is ABSOLUTE; 0 = removal).
CREATE TABLE l2_updates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id       TEXT NOT NULL,
    connection_epoch INTEGER NOT NULL,
    sequence_num     INTEGER,
    update_kind      TEXT NOT NULL,        -- snapshot | update
    side             TEXT NOT NULL,        -- bid | offer
    price_level      REAL NOT NULL,
    new_quantity     REAL NOT NULL,
    event_time_us    INTEGER,
    recv_time_us     INTEGER NOT NULL,
    raw_id           INTEGER NOT NULL
);
CREATE INDEX idx_l2_time ON l2_updates(product_id, recv_time_us);

-- Top-of-book quotes (from the ticker channel: best bid/ask + sizes).
CREATE TABLE quotes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      TEXT NOT NULL,
    best_bid        REAL,
    best_bid_qty    REAL,
    best_ask        REAL,
    best_ask_qty    REAL,
    event_time_us   INTEGER,
    recv_time_us    INTEGER NOT NULL,
    raw_id          INTEGER NOT NULL
);
CREATE INDEX idx_quotes_time ON quotes(product_id, recv_time_us);

-- Stream-health records: gaps, reconnects, crossed/stale books.
CREATE TABLE gaps (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    channel          TEXT NOT NULL,
    connection_epoch INTEGER NOT NULL,
    kind             TEXT NOT NULL,        -- GAP | RECONNECT | CROSSED | STALE | PARSE_ERROR
    last_seq         INTEGER,
    new_seq          INTEGER,
    detected_us      INTEGER NOT NULL,
    note             TEXT
);

-- Executable forward labels (see contract §8).
CREATE TABLE labels (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id                  TEXT NOT NULL,
    decision_time_us            INTEGER NOT NULL,
    horizon                     TEXT NOT NULL,
    entry_side                  TEXT,
    entry_price                 REAL,
    exit_side                   TEXT,
    exit_price                  REAL,
    gross_return                REAL,
    fee_component               REAL,
    slippage_component          REAL,
    adverse_selection_component REAL,
    spread_component            REAL,
    net_return                  REAL,
    mfe                         REAL,
    mae                         REAL,
    valid                       INTEGER NOT NULL,   -- 1/0
    invalid_reason              TEXT,
    quote_source                TEXT,
    sensitivity                 REAL NOT NULL DEFAULT 1.0,
    cost_model_version          TEXT NOT NULL,
    replay_version              TEXT NOT NULL
);
CREATE INDEX idx_labels_dt ON labels(product_id, decision_time_us, horizon);

-- Feature rows (see contract §11). value is NULL when missing/stale (never imputed).
CREATE TABLE features (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    version       TEXT NOT NULL,
    product_id    TEXT NOT NULL,
    event_time_us INTEGER NOT NULL,
    value         REAL,
    freshness_us  INTEGER,
    flags         TEXT,
    inputs_hash   TEXT
);
CREATE INDEX idx_features_name_time ON features(name, product_id, event_time_us);

-- Variant registry: the denominator for multiple-testing (contract §11/§13).
CREATE TABLE variant_registry (
    variant_id    TEXT PRIMARY KEY,
    track         TEXT NOT NULL,           -- microstructure | context
    name          TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    registered_us INTEGER NOT NULL
);
"""

MIGRATIONS: List[Tuple[int, str]] = [
    (1, _V1),
    (2, """
-- Make derived computations idempotent across repeated derive runs.
DELETE FROM labels
WHERE id NOT IN (
    SELECT MIN(id) FROM labels
    GROUP BY product_id, decision_time_us, horizon, sensitivity,
             cost_model_version, replay_version
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_labels_identity
ON labels(product_id, decision_time_us, horizon, sensitivity,
          cost_model_version, replay_version);

DELETE FROM features
WHERE id NOT IN (
    SELECT MIN(id) FROM features
    GROUP BY name, version, product_id, event_time_us, inputs_hash
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_features_identity
ON features(name, version, product_id, event_time_us, inputs_hash);
"""),
    (3, """
CREATE TABLE context_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id            TEXT NOT NULL,
    source_kind          TEXT NOT NULL,
    native_id            TEXT NOT NULL,
    vintage              TEXT NOT NULL,
    availability_time_us INTEGER NOT NULL,
    event_time_us        INTEGER,
    url                   TEXT NOT NULL,
    parser_version       TEXT NOT NULL,
    payload_sha256       TEXT NOT NULL,
    payload              TEXT NOT NULL,
    UNIQUE(source_id, native_id, vintage)
);
CREATE INDEX idx_context_availability
ON context_events(source_kind, availability_time_us);
"""),
    (4, """
-- Wide, versioned order-book mathematics sampled at registered decision times.
-- Public Level2 is aggregated, so queue/fill fields are estimates rather than
-- claims about exact individual-order position.
CREATE TABLE order_math (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_version           TEXT NOT NULL,
    product_id                TEXT NOT NULL,
    event_time_us             INTEGER NOT NULL,
    freshness_us              INTEGER,
    connection_epoch          INTEGER,
    best_bid                  REAL,
    best_ask                  REAL,
    mid                       REAL,
    spread_bps                REAL,
    best_bid_qty              REAL,
    best_ask_qty              REAL,
    queue_imbalance           REAL,
    multilevel_depth_imbalance REAL,
    microprice                REAL,
    microprice_delta_bps      REAL,
    bid_depth_5bps            REAL,
    ask_depth_5bps            REAL,
    depth_imbalance_5bps      REAL,
    bid_depth_10bps           REAL,
    ask_depth_10bps           REAL,
    depth_imbalance_10bps     REAL,
    bid_depth_25bps           REAL,
    ask_depth_25bps           REAL,
    depth_imbalance_25bps     REAL,
    bid_depth_50bps           REAL,
    ask_depth_50bps           REAL,
    depth_imbalance_50bps     REAL,
    ofi_60s                   REAL,
    mlofi_5_60s               REAL,
    mlofi_10_60s              REAL,
    bid_add_rate_60s          REAL,
    bid_deplete_rate_60s      REAL,
    ask_add_rate_60s          REAL,
    ask_deplete_rate_60s      REAL,
    bid_replenishment_ratio   REAL,
    ask_replenishment_ratio   REAL,
    bid_book_slope            REAL,
    ask_book_slope            REAL,
    bid_book_convexity        REAL,
    ask_book_convexity        REAL,
    flags                     TEXT NOT NULL,
    inputs_hash               TEXT NOT NULL,
    UNIQUE(feature_version, product_id, event_time_us)
);
CREATE INDEX idx_order_math_product_time
ON order_math(product_id, event_time_us);
"""),
]


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply all migrations newer than the DB's user_version. Returns final version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in MIGRATIONS:
        if version > current:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
            current = version
    return current
