"""
ResearchStore — the immutable ingestion + normalized storage API.

Stdlib only (sqlite3, hashlib, json). Imports nothing from bot/. Opens its OWN
database file; refuses to open a live/paper journal (defence in depth for the
safety boundary — see IMPLEMENTATION_CONTRACT.md §16).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from .schema import apply_migrations

# Journal databases owned by the live bot — the research store must never open these.
_FORBIDDEN_DB_NAMES = {"journal.db", "live_journal.db", "paper_journal.db"}


class AppendOnlyError(RuntimeError):
    """Raised when code attempts to mutate append-only raw evidence."""


def canonical_json(payload: Any) -> str:
    """Deterministic JSON: UTF-8, sorted keys, no insignificant whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def now_us() -> int:
    return int(time.time() * 1_000_000)


class ResearchStore:
    def __init__(self, db_path: str):
        base = os.path.basename(db_path).lower()
        if base in _FORBIDDEN_DB_NAMES:
            raise AppendOnlyError(
                f"Refusing to open '{db_path}': that is a live/paper journal database. "
                "The research store must use its own file (contract §16)."
            )
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        apply_migrations(self.conn)

    # --- registry / ledger -------------------------------------------------
    def register_source(
        self, source_id: str, source_kind: str, endpoint: str,
        schema_version: int = 1, notes: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO sources "
            "(source_id, source_kind, endpoint, schema_version, first_seen_us, notes) "
            "VALUES (?,?,?,?,?,?)",
            (source_id, source_kind, endpoint, schema_version, now_us(), notes),
        )
        self.conn.commit()

    def start_run(self, collector: str, params: Dict[str, Any]) -> str:
        run_id = f"run_{now_us()}_{os.getpid()}"
        self.conn.execute(
            "INSERT INTO ingestion_runs (run_id, collector, params_json, started_us, status) "
            "VALUES (?,?,?,?,'RUNNING')",
            (run_id, collector, canonical_json(params), now_us()),
        )
        self.conn.commit()
        return run_id

    def end_run(self, run_id: str, status: str, error: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE ingestion_runs SET ended_us=?, status=?, error=? WHERE run_id=?",
            (now_us(), status, error, run_id),
        )
        self.conn.commit()

    # --- raw append-only ---------------------------------------------------
    def append_raw(
        self, run_id: str, source_id: str, channel: str, payload: Any,
        event_time_us: Optional[int], recv_time_us: int,
        sequence_num: Optional[int] = None, connection_epoch: int = 0,
    ) -> Tuple[Optional[int], bool]:
        """Append a raw frame. Returns (raw_id, inserted). Duplicate hash => (existing_id, False)."""
        sha = payload_sha256(payload)
        text = canonical_json(payload)
        try:
            cur = self.conn.execute(
                "INSERT INTO raw_events "
                "(run_id, source_id, channel, payload_sha256, payload, event_time_us, "
                " recv_time_us, ingest_time_us, sequence_num, connection_epoch) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, source_id, channel, sha, text, event_time_us,
                 recv_time_us, now_us(), sequence_num, connection_epoch),
            )
            self.conn.execute(
                "UPDATE ingestion_runs SET raw_count = raw_count + 1 WHERE run_id=?",
                (run_id,),
            )
            self.conn.commit()
            return cur.lastrowid, True
        except sqlite3.IntegrityError:
            row = self.conn.execute(
                "SELECT id FROM raw_events WHERE payload_sha256=?", (sha,)
            ).fetchone()
            return (row["id"] if row else None), False

    # --- normalized inserts ------------------------------------------------
    def insert_trade(self, product_id: str, trade_id: str, price: float, size: float,
                     side: str, event_time_us: int, recv_time_us: int, raw_id: int) -> bool:
        try:
            self.conn.execute(
                "INSERT INTO trades (product_id, trade_id, price, size, side, "
                "event_time_us, recv_time_us, raw_id) VALUES (?,?,?,?,?,?,?,?)",
                (product_id, trade_id, price, size, side, event_time_us, recv_time_us, raw_id),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # idempotent (product_id, trade_id)

    def insert_l2_update(self, product_id: str, connection_epoch: int, sequence_num: Optional[int],
                         update_kind: str, side: str, price_level: float, new_quantity: float,
                         event_time_us: Optional[int], recv_time_us: int, raw_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO l2_updates (product_id, connection_epoch, sequence_num, update_kind, "
            "side, price_level, new_quantity, event_time_us, recv_time_us, raw_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (product_id, connection_epoch, sequence_num, update_kind, side, price_level,
             new_quantity, event_time_us, recv_time_us, raw_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def insert_l2_updates(self, rows: List[Tuple[Any, ...]]) -> int:
        """Insert one L2 frame atomically.

        A BTC-USD snapshot can contain more than 40,000 levels. Committing each
        level separately turns one frame into tens of thousands of transactions
        and makes the collector appear hung. Batch insertion keeps frame-level
        provenance and makes the public Level2 feed operational.
        """
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT INTO l2_updates (product_id, connection_epoch, sequence_num, "
            "update_kind, side, price_level, new_quantity, event_time_us, "
            "recv_time_us, raw_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def insert_quote(self, product_id: str, best_bid: Optional[float], best_bid_qty: Optional[float],
                     best_ask: Optional[float], best_ask_qty: Optional[float],
                     event_time_us: Optional[int], recv_time_us: int, raw_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO quotes (product_id, best_bid, best_bid_qty, best_ask, best_ask_qty, "
            "event_time_us, recv_time_us, raw_id) VALUES (?,?,?,?,?,?,?,?)",
            (product_id, best_bid, best_bid_qty, best_ask, best_ask_qty,
             event_time_us, recv_time_us, raw_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def record_gap(self, channel: str, connection_epoch: int, kind: str,
                   last_seq: Optional[int] = None, new_seq: Optional[int] = None,
                   note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO gaps (channel, connection_epoch, kind, last_seq, new_seq, detected_us, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (channel, connection_epoch, kind, last_seq, new_seq, now_us(), note),
        )
        self.conn.commit()

    def insert_label(self, label: Dict[str, Any]) -> int:
        cols = ("product_id", "decision_time_us", "horizon", "entry_side", "entry_price",
                "exit_side", "exit_price", "gross_return", "fee_component", "slippage_component",
                "adverse_selection_component", "spread_component", "net_return", "mfe", "mae",
                "valid", "invalid_reason", "quote_source", "sensitivity",
                "cost_model_version", "replay_version")
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO labels ({','.join(cols)}) "
            f"VALUES ({','.join('?' for _ in cols)})",
            tuple(label.get(c) for c in cols),
        )
        self.conn.commit()
        return cur.lastrowid if cur.rowcount else 0

    def insert_feature(self, name: str, version: str, product_id: str, event_time_us: int,
                       value: Optional[float], freshness_us: Optional[int],
                       flags: str = "", inputs_hash: str = "") -> int:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO features "
            "(name, version, product_id, event_time_us, value, "
            "freshness_us, flags, inputs_hash) VALUES (?,?,?,?,?,?,?,?)",
            (name, version, product_id, event_time_us, value, freshness_us, flags, inputs_hash),
        )
        self.conn.commit()
        return cur.lastrowid if cur.rowcount else 0

    def register_variant(self, variant_id: str, track: str, name: str, params: Dict[str, Any]) -> bool:
        try:
            self.conn.execute(
                "INSERT INTO variant_registry (variant_id, track, name, params_json, registered_us) "
                "VALUES (?,?,?,?,?)",
                (variant_id, track, name, canonical_json(params), now_us()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def insert_context(self, record: Dict[str, Any]) -> bool:
        payload = record["payload"]
        try:
            self.conn.execute(
                "INSERT INTO context_events "
                "(source_id, source_kind, native_id, vintage, availability_time_us, "
                "event_time_us, url, parser_version, payload_sha256, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    record["source_id"],
                    record["source_kind"],
                    record["native_id"],
                    record["vintage"],
                    record["availability_time_us"],
                    record.get("event_time_us"),
                    record["url"],
                    record["parser_version"],
                    record.get("payload_sha256") or payload_sha256(payload),
                    canonical_json(payload),
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def insert_order_math(self, record: Dict[str, Any]) -> int:
        cols = (
            "feature_version", "product_id", "event_time_us", "freshness_us",
            "connection_epoch", "best_bid", "best_ask", "mid", "spread_bps",
            "best_bid_qty", "best_ask_qty", "queue_imbalance",
            "multilevel_depth_imbalance", "microprice",
            "microprice_delta_bps", "bid_depth_5bps", "ask_depth_5bps",
            "depth_imbalance_5bps", "bid_depth_10bps", "ask_depth_10bps",
            "depth_imbalance_10bps", "bid_depth_25bps", "ask_depth_25bps",
            "depth_imbalance_25bps", "bid_depth_50bps", "ask_depth_50bps",
            "depth_imbalance_50bps", "ofi_60s", "mlofi_5_60s",
            "mlofi_10_60s", "bid_add_rate_60s",
            "bid_deplete_rate_60s", "ask_add_rate_60s",
            "ask_deplete_rate_60s", "bid_replenishment_ratio",
            "ask_replenishment_ratio", "bid_book_slope", "ask_book_slope",
            "bid_book_convexity", "ask_book_convexity", "flags",
            "inputs_hash",
        )
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO order_math ({','.join(cols)}) "
            f"VALUES ({','.join('?' for _ in cols)})",
            tuple(record.get(col) for col in cols),
        )
        self.conn.commit()
        return cur.lastrowid if cur.rowcount else 0

    # --- queries -----------------------------------------------------------
    def count(self, table: str) -> int:
        # table name is from a fixed internal set; never user input.
        return self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]

    def count_variants(self, track: Optional[str] = None) -> int:
        if track:
            return self.conn.execute(
                "SELECT COUNT(*) AS c FROM variant_registry WHERE track=?", (track,)
            ).fetchone()["c"]
        return self.count("variant_registry")

    def get_quotes(self, product_id: str, start_us: int, end_us: int) -> List[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM quotes WHERE product_id=? AND recv_time_us BETWEEN ? AND ? "
            "ORDER BY recv_time_us ASC",
            (product_id, start_us, end_us),
        ))

    def iter_raw(self, channel: Optional[str] = None):
        if channel:
            cur = self.conn.execute(
                "SELECT * FROM raw_events WHERE channel=? ORDER BY id ASC", (channel,))
        else:
            cur = self.conn.execute("SELECT * FROM raw_events ORDER BY id ASC")
        yield from cur

    def storage_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                total += os.path.getsize(p)
        return total

    # --- retention ---------------------------------------------------------
    def prune_derived(self, before_us: int, allow_raw_prune: bool = False) -> Dict[str, int]:
        """Prune derived rows older than before_us. NEVER deletes raw evidence unless
        allow_raw_prune is True AND every raw row has been exported (not implemented here,
        so raw is never pruned). Returns counts deleted per table."""
        deleted = {}
        for table in ("features", "labels"):
            cur = self.conn.execute(
                f"DELETE FROM {table} WHERE "
                + ("event_time_us" if table == "features" else "decision_time_us")
                + " < ?",
                (before_us,),
            )
            deleted[table] = cur.rowcount
        self.conn.commit()
        if allow_raw_prune:
            # Raw evidence is append-only and not yet exportable; refuse loudly.
            raise AppendOnlyError(
                "allow_raw_prune=True but raw export is not implemented; refusing to "
                "delete unexported raw evidence (contract §6)."
            )
        deleted["raw_events"] = 0
        return deleted

    def close(self) -> None:
        self.conn.close()
