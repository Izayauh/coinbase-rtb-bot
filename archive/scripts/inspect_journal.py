#!/usr/bin/env python
"""
inspect_journal.py — inspect paper_journal.db after a run.

Usage:
    python inspect_journal.py
    python inspect_journal.py --db paper_journal.db --events 100 --equity 20
"""
import argparse
import json
import sqlite3
from datetime import datetime, timezone

DEFAULTS = {
    "db": "paper_journal.db",
    "events": 50,
    "equity": 10,
}


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def show_events(conn, limit: int) -> None:
    _section(f"Recent Events (last {limit})")
    rows = conn.execute(
        "SELECT id, ts, event_type, message FROM event_log "
        "ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        print("  (no events)")
        return
    for r in reversed(rows):
        payload = json.loads(r["message"]) if r["message"] else {}
        payload_str = "  " + json.dumps(payload) if payload else ""
        print(f"  [{_fmt_ts(r['ts'])}] {r['event_type']}{payload_str}")


def show_event_counts(conn) -> None:
    _section("Event Counts (all time)")
    rows = conn.execute(
        "SELECT event_type, COUNT(*) AS cnt FROM event_log "
        "GROUP BY event_type ORDER BY cnt DESC"
    ).fetchall()
    if not rows:
        print("  (no events)")
        return
    for r in rows:
        print(f"  {r['event_type']:<30} {r['cnt']}")


def show_positions(conn) -> None:
    _section("Positions")
    rows = conn.execute("SELECT * FROM positions ORDER BY state, symbol").fetchall()
    if not rows:
        print("  (none)")
        return
    for r in rows:
        stop = f"stop={r['stop_price']:.2f} active={bool(r['stop_active'])}" if r["stop_price"] else ""
        pnl = f"realized={r['realized_pnl']:.4f} unrealized={r['unrealized_pnl']:.4f}"
        print(
            f"  {r['symbol']} [{r['state']}]  "
            f"size={r['current_size']}  avg_entry={r['avg_entry']:.2f}  "
            f"{pnl}  {stop}"
        )


def show_orders(conn, limit: int = 20) -> None:
    _section(f"Recent Orders (last {limit})")
    rows = conn.execute(
        "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        print("  (none)")
        return
    for r in rows:
        exch = f"  exch={r['exchange_order_id']}" if r["exchange_order_id"] else ""
        fail = f"  fail={r['fail_reason']}" if r["fail_reason"] else ""
        print(
            f"  [{_fmt_ts(r['created_at'])}] {r['order_id']}  "
            f"{r['symbol']} {r['side']}  "
            f"size={r['size']}  filled={r['executed_size']}  "
            f"status={r['status']}{exch}{fail}"
        )


def show_equity(conn, limit: int) -> None:
    _section(f"Equity Snapshots (last {limit})")
    rows = conn.execute(
        "SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        print("  (no snapshots yet — equity_snapshot_task runs every 60s)")
        return
    for r in reversed(rows):
        print(
            f"  [{_fmt_ts(r['ts'])}]  "
            f"equity={r['total_equity']:.2f}  "
            f"unrealized={r['unrealized_pnl']:.4f}  "
            f"realized={r['realized_pnl']:.4f}  "
            f"open_pos={r['open_positions']}"
        )


def show_safeguard_state(conn) -> None:
    _section("Safeguard State")
    row = conn.execute(
        "SELECT value FROM runtime_state WHERE key='safeguards'"
    ).fetchone()
    if not row:
        print("  (no persisted state)")
        return
    state = json.loads(row["value"])
    enabled = state.get("trading_enabled", "?")
    tripped = state.get("tripped", [])
    print(f"  trading_enabled : {enabled}")
    print(f"  tripped guards  : {tripped if tripped else '(none)'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect paper trading journal DB")
    parser.add_argument("--db", default=DEFAULTS["db"], help="Path to SQLite DB file")
    parser.add_argument("--events", type=int, default=DEFAULTS["events"], help="Number of events to show")
    parser.add_argument("--equity", type=int, default=DEFAULTS["equity"], help="Number of equity snapshots to show")
    args = parser.parse_args()

    try:
        conn = _conn(args.db)
    except Exception as e:
        print(f"Cannot open database '{args.db}': {e}")
        return

    print(f"\nJournal: {args.db}")

    show_safeguard_state(conn)
    show_positions(conn)
    show_orders(conn)
    show_equity(conn, args.equity)
    show_event_counts(conn)
    show_events(conn, args.events)

    conn.close()


if __name__ == "__main__":
    main()
