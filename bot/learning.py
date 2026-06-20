"""Outcome-backed learning journal.

The learning loop records reconciled realized outcomes and updates descriptive
reviews.  It never changes strategy parameters or grants live authority.
"""
from __future__ import annotations

import statistics
import time
from typing import Any

from .db import db


def record_trade_outcome(
    *,
    symbol: str,
    strategy_id: str,
    strategy_version: str,
    entry_order_id: str,
    exit_order_id: str,
    entry_ts: int,
    exit_ts: int,
    quantity: float,
    avg_entry: float,
    avg_exit: float,
    entry_fee: float,
    exit_fee: float,
    exit_reason: str,
    position_closed: bool,
) -> dict[str, Any]:
    """Insert one immutable realized outcome and refresh its strategy review."""
    gross_pnl = (avg_exit - avg_entry) * quantity
    net_pnl = gross_pnl - entry_fee - exit_fee
    entry_notional = avg_entry * quantity
    return_bps = (net_pnl / entry_notional * 10_000.0) if entry_notional > 0 else 0.0
    holding_seconds = max(0, int(exit_ts) - int(entry_ts))
    outcome_id = f"{entry_order_id}:{exit_order_id}"
    created_at = int(time.time())

    db.execute(
        """
        INSERT OR IGNORE INTO trade_outcomes (
            outcome_id, symbol, strategy_id, strategy_version, entry_order_id,
            exit_order_id, entry_ts, exit_ts, quantity, avg_entry, avg_exit,
            entry_fee, exit_fee, gross_pnl, net_pnl, return_bps,
            holding_seconds, exit_reason, position_closed, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            outcome_id,
            symbol,
            strategy_id,
            strategy_version,
            entry_order_id,
            exit_order_id,
            int(entry_ts),
            int(exit_ts),
            float(quantity),
            float(avg_entry),
            float(avg_exit),
            float(entry_fee),
            float(exit_fee),
            float(gross_pnl),
            float(net_pnl),
            float(return_bps),
            holding_seconds,
            exit_reason,
            1 if position_closed else 0,
            created_at,
        ),
    )
    refresh_learning_review(strategy_id, strategy_version)
    return {
        "outcome_id": outcome_id,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "return_bps": return_bps,
        "holding_seconds": holding_seconds,
        "position_closed": position_closed,
    }


def refresh_learning_review(
    strategy_id: str,
    strategy_version: str,
    *,
    min_samples: int = 20,
) -> dict[str, Any]:
    """Recompute a descriptive, non-authorizing strategy outcome review."""
    rows = db.fetch_all(
        """
        SELECT net_pnl, return_bps
        FROM trade_outcomes
        WHERE strategy_id=? AND strategy_version=?
        ORDER BY exit_ts, outcome_id
        """,
        (strategy_id, strategy_version),
    )
    pnls = [float(r["net_pnl"]) for r in rows]
    returns = [float(r["return_bps"]) for r in rows]
    sample_count = len(rows)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / sample_count if sample_count else 0.0
    expectancy_bps = statistics.fmean(returns) if returns else 0.0
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else None

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    if sample_count < min_samples:
        status = "COLLECTING"
    elif expectancy_bps > 0 and (profit_factor is None or profit_factor > 1.0):
        status = "RESEARCH_REVIEW_CANDIDATE"
    else:
        status = "DEMOTION_REVIEW"

    generated_at = int(time.time())
    db.execute(
        """
        INSERT INTO learning_reviews (
            strategy_id, strategy_version, sample_count, win_rate,
            expectancy_bps, profit_factor, total_net_pnl, max_drawdown_usd,
            status, generated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(strategy_id, strategy_version) DO UPDATE SET
            sample_count=excluded.sample_count,
            win_rate=excluded.win_rate,
            expectancy_bps=excluded.expectancy_bps,
            profit_factor=excluded.profit_factor,
            total_net_pnl=excluded.total_net_pnl,
            max_drawdown_usd=excluded.max_drawdown_usd,
            status=excluded.status,
            generated_at=excluded.generated_at
        """,
        (
            strategy_id,
            strategy_version,
            sample_count,
            win_rate,
            expectancy_bps,
            profit_factor,
            sum(pnls),
            max_drawdown,
            status,
            generated_at,
        ),
    )
    return {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "sample_count": sample_count,
        "win_rate": win_rate,
        "expectancy_bps": expectancy_bps,
        "profit_factor": profit_factor,
        "total_net_pnl": sum(pnls),
        "max_drawdown_usd": max_drawdown,
        "status": status,
        "automatic_parameter_changes": False,
        "live_authority_granted": False,
    }
