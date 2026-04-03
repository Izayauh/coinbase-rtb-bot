from dataclasses import dataclass
from typing import Optional

@dataclass
class Bar:
    symbol: str
    timeframe: str
    ts_open: int  # Unix timestamp in seconds
    open: float
    high: float
    low: float
    close: float
    volume: float

@dataclass
class Signal:
    signal_id: str
    symbol: str
    signal_type: str
    regime_snapshot: str  # JSON payload recording indicators at time of firing
    breakout_level: float
    retest_level: float
    atr: float
    rsi: float
    status: str
    execution_price: float

@dataclass
class Order:
    order_id: str
    signal_id: str
    symbol: str
    side: str
    price: float
    size: float
    executed_size: float
    status: str
    created_at: int
    exchange_order_id: Optional[str] = None
    submitted_at: Optional[int] = None
    updated_at: Optional[int] = None
    fail_reason: Optional[str] = None

@dataclass
class Position:
    symbol: str
    entry_ts: int
    avg_entry: float
    current_size: float
    realized_pnl: float
    unrealized_pnl: float
    stop_price: float
    state: str
    stop_active: bool = False

@dataclass
class Execution:
    execution_id: str
    order_id: str
    price: float
    size: float
    fee: float
    ts: int
