from dataclasses import dataclass

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

@dataclass
class Execution:
    execution_id: str
    order_id: str
    price: float
    size: float
    fee: float
    ts: int
