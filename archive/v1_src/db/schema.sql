CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT,
    timeframe TEXT,
    ts_open INTEGER,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    PRIMARY KEY (symbol, timeframe, ts_open)
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    symbol TEXT,
    signal_type TEXT,
    regime_snapshot TEXT,
    breakout_level REAL,
    retest_level REAL,
    atr REAL,
    rsi REAL,
    expected_rr REAL,
    status TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    order_id_internal TEXT PRIMARY KEY,
    exchange_order_id TEXT,
    client_order_id TEXT,
    symbol TEXT,
    side TEXT,
    order_type TEXT,
    tif TEXT,
    price REAL,
    size REAL,
    status TEXT,
    linked_signal_id TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT,
    fill_ts INTEGER,
    fill_price REAL,
    fill_size REAL,
    fee REAL,
    liquidity_side TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    entry_ts INTEGER,
    avg_entry REAL,
    current_size REAL,
    realized_pnl REAL,
    unrealized_pnl REAL,
    stop_price REAL,
    trail_price REAL,
    state TEXT
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER,
    event_type TEXT,
    symbol TEXT,
    message TEXT,
    action_taken TEXT
);
