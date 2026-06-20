"""
research/types.py — Core data types for the Lane A research harness.

All types are plain dataclasses. No dependency on bot/.
"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Bar:
    """Single OHLCV bar."""
    symbol: str
    timeframe: str
    ts: int          # open timestamp (unix seconds)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Signal:
    """A trading signal emitted by a rule on a specific bar."""
    bar_index: int          # index into the bar array where signal fires
    direction: str          # "long" or "short"
    rule_name: str          # which rule generated this
    params: dict = field(default_factory=dict)


@dataclass
class Trade:
    """A completed round-trip trade."""
    entry_bar: int          # bar index of entry
    exit_bar: int           # bar index of exit
    direction: str          # "long" or "short"
    entry_price: float
    exit_price: float
    stop_price: float
    size: float             # position size in base units
    pnl_dollar: float       # after fees
    pnl_pct: float          # after fees, as fraction (0.01 = 1%)
    entry_cost: float       # total friction paid at entry (dollars)
    exit_cost: float        # total friction paid at exit (dollars)
    bars_held: int
    exit_reason: str        # "STOP_LOSS", "TAKE_PROFIT", "TIME_STOP"
    mae_pct: float = 0.0   # max adverse excursion (worst unrealized loss, %)
    mfe_pct: float = 0.0   # max favorable excursion (best unrealized gain, %)


@dataclass
class BacktestResult:
    """Aggregate result of a single backtest run."""
    symbol: str
    timeframe: str
    rule_name: str
    params: dict
    trades: List[Trade]
    equity_curve: List[float]

    # Pre-computed summary metrics
    trade_count: int = 0
    win_count: int = 0
    win_rate: float = 0.0
    avg_pnl_pct: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_bars_held: float = 0.0
    expectancy_pct: float = 0.0
    total_return_pct: float = 0.0
    sharpe: float = 0.0

    def compute_metrics(self, initial_equity: float = 10000.0):
        """Fill summary fields from the trades list."""
        n = len(self.trades)
        self.trade_count = n
        if n == 0:
            return

        wins = [t for t in self.trades if t.pnl_pct > 0]
        losses = [t for t in self.trades if t.pnl_pct <= 0]
        self.win_count = len(wins)
        self.win_rate = self.win_count / n

        pnls = [t.pnl_pct for t in self.trades]
        self.avg_pnl_pct = sum(pnls) / n

        gross_profit = sum(t.pnl_pct for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl_pct for t in losses)) if losses else 0
        self.profit_factor = (
            gross_profit / gross_loss if gross_loss > 0
            else float("inf") if gross_profit > 0
            else 0.0
        )

        # Max drawdown from equity curve
        if self.equity_curve:
            peak = self.equity_curve[0]
            max_dd = 0.0
            for eq in self.equity_curve:
                if eq > peak:
                    peak = eq
                dd = (peak - eq) / peak if peak > 0 else 0
                max_dd = max(max_dd, dd)
            self.max_drawdown_pct = max_dd * 100

        self.avg_bars_held = sum(t.bars_held for t in self.trades) / n

        # Expectancy
        avg_w = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0
        avg_l = abs(sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0
        self.expectancy_pct = self.win_rate * avg_w - (1 - self.win_rate) * avg_l

        # Total return
        final = self.equity_curve[-1] if self.equity_curve else initial_equity
        self.total_return_pct = (final / initial_equity - 1) * 100

        # Sharpe (annualized, assuming 1h bars → ~8760 bars/year)
        import math
        if n >= 2:
            mean_r = sum(pnls) / n
            var_r = sum((p - mean_r) ** 2 for p in pnls) / (n - 1)
            std_r = math.sqrt(var_r) if var_r > 0 else 1e-10
            # trades_per_year estimate
            if self.trades[-1].exit_bar > self.trades[0].entry_bar:
                bar_span = self.trades[-1].exit_bar - self.trades[0].entry_bar
                trades_per_year = n / bar_span * 8760
            else:
                trades_per_year = n
            self.sharpe = (mean_r / std_r) * math.sqrt(trades_per_year)
