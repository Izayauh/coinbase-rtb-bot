"""Executable tiny-live acceptance gates.

This module is read-only with respect to the exchange.  It may open SQLite
databases in read-only mode and request a Coinbase order preview, but it never
submits, cancels, or modifies an order.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import ctypes
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import config
from .credentials import refresh_coinbase_credentials_from_user_environment
from .readiness import (
    _preview_configured_live_buy,
    parse_coinbase_balances,
)
from .strategy_authorization import validate_configured_authorization
from .strategy_registry import implementation_evidence
from .acceptance_receipt import compute_acceptance_hash


@dataclass
class GateResult:
    name: str
    passed: bool
    summary: str
    evidence: dict[str, Any]
    required: bool = True


def _result(
    name: str,
    passed: bool,
    summary: str,
    evidence: dict[str, Any] | None = None,
    *,
    required: bool = True,
) -> GateResult:
    return GateResult(name, passed, summary, evidence or {}, required)


def _resolve(path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else Path.cwd() / candidate


def _live_db_connection() -> sqlite3.Connection:
    path = _resolve(config.live_db_path())
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _latest_bar(conn: sqlite3.Connection, timeframe: str) -> tuple[int, int]:
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(MAX(ts_open), 0) AS latest "
        "FROM bars WHERE symbol=? AND timeframe=?",
        (config.symbol(), timeframe),
    ).fetchone()
    return int(row["n"]), int(row["latest"])


def _recent_contiguous(
    conn: sqlite3.Connection,
    timeframe: str,
    interval: int,
    minimum: int,
) -> bool:
    rows = conn.execute(
        "SELECT ts_open FROM bars WHERE symbol=? AND timeframe=? "
        "ORDER BY ts_open DESC LIMIT ?",
        (config.symbol(), timeframe, minimum),
    ).fetchall()
    stamps = sorted(int(r["ts_open"]) for r in rows)
    return len(stamps) >= minimum and all(
        stamps[i] - stamps[i - 1] == interval for i in range(1, len(stamps))
    )


def gate_tiny_caps() -> GateResult:
    order_cap = config.max_order_size_usd()
    position_cap = config.max_position_size_usd()
    test_notional = config.live_test_order_notional_usd()
    passed = (
        config.runtime_mode() == "live"
        and config.symbols() == ["BTC-USD"]
        and config.product_allowlist() == ["BTC-USD"]
        and 0 < test_notional <= order_cap <= 15.0
        and 0 < position_cap <= 30.0
    )
    return _result(
        "tiny_caps",
        passed,
        "tiny live caps are bounded" if passed else "tiny live caps are not bounded",
        {
            "mode": config.runtime_mode(),
            "symbols": config.symbols(),
            "allowlist": config.product_allowlist(),
            "test_order_notional_usd": test_notional,
            "max_order_size_usd": order_cap,
            "max_position_size_usd": position_cap,
        },
    )


def gate_entry_halt() -> GateResult:
    path = _resolve(config.kill_switch_file())
    absent = not path.exists()
    return _result(
        "entry_halt",
        absent,
        "entry halt is absent" if absent else "entry halt is intentionally active",
        {
            "path": str(path),
            "present": not absent,
            "exits_remain_enabled": True,
        },
    )


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def gate_runtime_process() -> GateResult:
    root = Path.cwd()
    pid_path = root / ".cb_rtb_bot.lock.pid"
    lock_path = root / ".cb_rtb_bot.lock"
    pid = 0
    source = ""
    for path in (pid_path, lock_path):
        try:
            pid = int(path.read_text(encoding="ascii").strip().splitlines()[0])
            source = str(path)
            break
        except (OSError, ValueError, IndexError):
            continue
    alive = _pid_is_alive(pid)
    return _result(
        "runtime_process",
        alive,
        "single managed runtime is alive" if alive else "managed runtime is not alive",
        {"pid": pid or None, "pid_source": source or None},
    )


def gate_strategy_authorization() -> GateResult:
    valid, reason, payload = validate_configured_authorization()
    return _result(
        "strategy_authorization",
        valid,
        reason,
        {
            "strategy_id": config.strategy_id(),
            "strategy_version": config.strategy_version(),
            "authorization_file": str(_resolve(config.strategy_authorization_file())),
            "evidence_status": payload.get("evidence_status") if payload else None,
            "expires_at": payload.get("expires_at") if payload else None,
        },
    )


def gate_strategy_implementation() -> GateResult:
    evidence = implementation_evidence(
        config.strategy_id(),
        config.strategy_version(),
    )
    passed = bool(evidence.get("implementation_ready"))
    return _result(
        "strategy_implementation",
        passed,
        "configured strategy has a verified live implementation"
        if passed
        else str(evidence.get("reason") or "strategy implementation is not ready"),
        evidence,
    )


def gate_exchange_preview() -> GateResult:
    refresh_coinbase_credentials_from_user_environment()
    api_key = os.environ.get("COINBASE_API_KEY", "")
    api_secret = os.environ.get("COINBASE_API_SECRET", "")
    if not api_key or not api_secret:
        return _result(
            "exchange_preview",
            False,
            "Coinbase credentials are missing",
            {"credentials_present": False},
        )
    try:
        from coinbase.rest import RESTClient

        client = RESTClient(api_key=api_key, api_secret=api_secret)
        balances = parse_coinbase_balances(client.get_accounts())
        preview_blocker = _preview_configured_live_buy(client)
    except Exception as exc:
        return _result(
            "exchange_preview",
            False,
            f"Coinbase read-only preflight failed: {exc}",
            {"credentials_present": True},
        )
    passed = bool(balances) and preview_blocker is None
    return _result(
        "exchange_preview",
        passed,
        "Coinbase account and non-executing BUY preview passed"
        if passed
        else (preview_blocker or "Coinbase returned no accounts"),
        {
            "account_count": len(balances),
            "configured_notional_usd": config.live_test_order_notional_usd(),
            "order_submitted": False,
        },
    )


def gate_live_database(now: int | None = None) -> GateResult:
    current = int(time.time()) if now is None else int(now)
    try:
        with _live_db_connection() as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            pending = [
                dict(r)
                for r in conn.execute(
                    "SELECT order_id, created_at, status FROM orders "
                    "WHERE status IN ('PENDING','PARTIAL')"
                )
            ]
            stale_pending = [
                r
                for r in pending
                if current - int(r.get("created_at") or current)
                > config.max_pending_order_age_sec()
            ]
            orphan_executions = conn.execute(
                "SELECT COUNT(*) FROM executions e LEFT JOIN orders o "
                "ON o.order_id=e.order_id WHERE o.order_id IS NULL"
            ).fetchone()[0]
            bad_positions = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE state='OPEN' AND "
                "(current_size<=0 OR stop_active!=1 OR stop_price<=0)"
            ).fetchone()[0]
    except Exception as exc:
        return _result(
            "reconciliation",
            False,
            f"live journal inspection failed: {exc}",
            {"db_path": str(_resolve(config.live_db_path()))},
        )

    passed = (
        integrity == "ok"
        and not stale_pending
        and orphan_executions == 0
        and bad_positions == 0
    )
    return _result(
        "reconciliation",
        passed,
        "live journal integrity and reconciliation invariants passed"
        if passed
        else "live journal reconciliation invariants failed",
        {
            "integrity": integrity,
            "pending_orders": len(pending),
            "stale_pending_orders": len(stale_pending),
            "orphan_executions": orphan_executions,
            "invalid_open_positions": bad_positions,
        },
    )


def gate_market_data(now: int | None = None) -> GateResult:
    current = int(time.time()) if now is None else int(now)
    try:
        with _live_db_connection() as conn:
            counts = {}
            ages = {}
            for timeframe in ("1m", "1h", "4h"):
                count, latest = _latest_bar(conn, timeframe)
                counts[timeframe] = count
                ages[timeframe] = current - latest if latest else None
            contiguous_1h = _recent_contiguous(conn, "1h", 3600, 25)
            contiguous_4h = _recent_contiguous(conn, "4h", 14400, 205)
    except Exception as exc:
        return _result("market_data", False, f"market-data gate failed: {exc}")

    passed = (
        ages["1m"] is not None
        and ages["1m"] <= 300
        and ages["1h"] is not None
        and ages["1h"] <= 7200
        and ages["4h"] is not None
        # A completed 4h bar is keyed by bucket start, so immediately before
        # the next close its timestamp can be almost 8h old. Allow 10h,
        # including a bounded two-hour ingestion/restart grace period.
        and ages["4h"] <= 36000
        and contiguous_1h
        and contiguous_4h
    )
    return _result(
        "market_data",
        passed,
        "live bars are fresh and strategy history is contiguous"
        if passed
        else "live bars are stale or non-contiguous",
        {
            "bar_counts": counts,
            "age_seconds": ages,
            "contiguous_1h_25": contiguous_1h,
            "contiguous_4h_205": contiguous_4h,
        },
    )


def gate_exit_path() -> GateResult:
    try:
        with _live_db_connection() as conn:
            invalid = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE state='OPEN' AND "
                "(stop_active!=1 OR stop_price<=0)"
            ).fetchone()[0]
            invalid_candidate_contracts = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE state='OPEN' "
                "AND strategy_id='btc_derivatives_stress_exhaustion' "
                "AND (target_price IS NULL OR target_price<=avg_entry "
                "OR time_stop_at IS NULL OR time_stop_at<=entry_ts)"
            ).fetchone()[0]
    except Exception as exc:
        return _result("exit_path", False, f"exit-path inspection failed: {exc}")
    live_cfg = config._raw.get("live", {}) or {}
    passed = (
        bool(live_cfg.get("auto_exit_enabled"))
        and invalid == 0
        and invalid_candidate_contracts == 0
    )
    return _result(
        "exit_path",
        passed,
        "automatic stop/target/time exits are enabled and positions are protected"
        if passed
        else "automatic exits are disabled or an open position lacks a stop",
        {
            "auto_exit_enabled": bool(live_cfg.get("auto_exit_enabled")),
            "take_profit_r": live_cfg.get("take_profit_r"),
            "time_stop_bars": live_cfg.get("time_stop_bars"),
            "invalid_open_positions": invalid,
            "invalid_candidate_exit_contracts": invalid_candidate_contracts,
            "entry_halt_blocks_exits": False,
        },
    )


def gate_learning_loop() -> GateResult:
    try:
        with _live_db_connection() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required = {"trade_outcomes", "learning_reviews"}
            missing = sorted(required - tables)
            outcomes = (
                conn.execute("SELECT COUNT(*) FROM trade_outcomes").fetchone()[0]
                if not missing
                else 0
            )
    except Exception as exc:
        return _result("learning_loop", False, f"learning-loop inspection failed: {exc}")
    passed = not missing
    return _result(
        "learning_loop",
        passed,
        "reconciled outcome journal and review tables are available"
        if passed
        else "learning schema is missing from the live journal",
        {
            "missing_tables": missing,
            "recorded_outcomes": outcomes,
            "automatic_parameter_changes": False,
            "live_authority_from_learning": False,
        },
    )


def gate_research_provenance() -> GateResult:
    path = _resolve(
        str(
            (config._raw.get("research") or {}).get(
                "db_path", "research_pipeline_data/research.db"
            )
        )
    )
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        sources = {
            str(r["source_id"]): int(r["n"])
            for r in conn.execute(
                "SELECT source_id, COUNT(*) AS n FROM context_events GROUP BY source_id"
            )
        }
        source_kinds = {
            str(r["source_kind"]): int(r["n"])
            for r in conn.execute(
                "SELECT source_kind, COUNT(*) AS n FROM context_events GROUP BY source_kind"
            )
        }
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("raw_events", "trades", "quotes", "l2_updates", "features", "labels")
        }
        gaps = conn.execute("SELECT COUNT(*) FROM gaps").fetchone()[0]
        conn.close()
    except Exception as exc:
        return _result(
            "research_provenance",
            False,
            f"research provenance inspection failed: {exc}",
            {"db_path": str(path)},
        )

    required_kinds = {
        "fomc",
        "cpi_bls",
        "edgar",
        "cftc",
        "official_exchange",
        "attributed_news",
    }
    missing_official = sorted(required_kinds - set(source_kinds))
    attributed_news = int(source_kinds.get("attributed_news", 0))
    passed = (
        not missing_official
        and attributed_news > 0
        and all(count > 0 for count in counts.values())
        and gaps == 0
    )
    return _result(
        "research_provenance",
        passed,
        "market, regulatory, and attributed-news provenance is populated"
        if passed
        else "research inputs are incomplete",
        {
            "sources": sources,
            "source_kinds": source_kinds,
            "missing_official_sources": missing_official,
            "attributed_news_records": attributed_news,
            "market_counts": counts,
            "gaps": gaps,
        },
    )


def gate_adversarial_tests() -> GateResult:
    tests = [
        "bot/tests/test_live_roundtrip.py",
        "bot/tests/test_reconcile.py",
        "bot/tests/test_forced_failures.py",
        "bot/tests/test_strategy_authorization.py",
        "bot/tests/test_strategy_registry.py",
        "bot/tests/test_authorize_strategy.py",
        "bot/tests/test_candidate_advisory_bridge.py",
        "bot/tests/test_main_strategy_selection.py",
        "bot/tests/test_acceptance_receipt.py",
        "bot/tests/test_learning.py",
        "bot/tests/test_single_instance.py",
        "research_pipeline/tests/test_boundary.py",
        "research_pipeline/tests/test_governance.py",
    ]
    result = subprocess.run(
        [str(Path(os.sys.executable)), "-m", "pytest", "-q", *tests],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return _result(
        "adversarial_tests",
        result.returncode == 0,
        "targeted strategy/execution/reconciliation/exit/safety tests passed"
        if result.returncode == 0
        else "targeted adversarial tests failed",
        {
            "returncode": result.returncode,
            "summary": (result.stdout + result.stderr)[-2000:],
        },
    )


def evaluate_tiny_live_acceptance(*, run_tests: bool = True) -> dict[str, Any]:
    gates = [
        gate_tiny_caps(),
        gate_runtime_process(),
        gate_entry_halt(),
        gate_strategy_implementation(),
        gate_strategy_authorization(),
        gate_exchange_preview(),
        gate_live_database(),
        gate_market_data(),
        gate_exit_path(),
        gate_learning_loop(),
        gate_research_provenance(),
    ]
    if run_tests:
        gates.append(gate_adversarial_tests())
    ready = all(g.passed for g in gates if g.required)
    return {
        "schema_version": 2,
        "generated_at": int(time.time()),
        "strategy_id": config.strategy_id(),
        "strategy_version": config.strategy_version(),
        "ready_for_tiny_live": ready,
        "decision": "READY" if ready else "BLOCKED",
        "gates": [asdict(g) for g in gates],
        "order_submitted": False,
    }


def write_report(report: dict[str, Any], path: str | Path) -> None:
    report["acceptance_hash"] = compute_acceptance_hash(report)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
