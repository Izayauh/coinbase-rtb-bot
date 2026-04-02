import logging
import uuid
from typing import List, Optional
from models import Bar, Signal
from strategy import Indicators, is_bullish_regime
from journal import Journal
from db import db

logger = logging.getLogger(__name__)

class StateMachine:
    # DEFINED STATES
    IDLE = "IDLE"
    WAITING_RETEST = "WAITING_RETEST"
    RETEST_CONFIRMED = "RETEST_CONFIRMED"
    SIGNAL_EMITTED = "SIGNAL_EMITTED"
    COOLDOWN = "COOLDOWN"  # Reserved explicitly for structural failures locally
    DISABLED = "DISABLED"

    def __init__(self):
        self.state = self.IDLE
        self.setup_id = None
        self.breakout_bar: Optional[Bar] = None
        self.retest_bar: Optional[Bar] = None
        self.bars_since_breakout = 0
        self.breakout_level = 0.0
        
        self.last_1h_ts = 0
        self.last_4h_ts = 0
        
        # Boot safely
        persisted = Journal.get_state("algo_state")
        if persisted:
            self.state = persisted.get("state", self.IDLE)
            self.setup_id = persisted.get("setup_id")
            self.last_1h_ts = persisted.get("last_1h_ts", 0)
            self.last_4h_ts = persisted.get("last_4h_ts", 0)
            self.bars_since_breakout = persisted.get("bars_since_breakout", 0)
            self.breakout_level = persisted.get("breakout_level", 0.0)
            
            bp = persisted.get("breakout_bar")
            if bp: self.breakout_bar = Bar(**bp)
            
            rp = persisted.get("retest_bar")
            if rp: self.retest_bar = Bar(**rp)
            
            logger.info(f"State Machine Booted - Restart State: {self.state}")

    def _persist(self):
        Journal.upsert_state("algo_state", {
            "state": self.state,
            "setup_id": self.setup_id,
            "last_1h_ts": self.last_1h_ts,
            "last_4h_ts": self.last_4h_ts,
            "bars_since_breakout": self.bars_since_breakout,
            "breakout_level": self.breakout_level,
            "breakout_bar": self.breakout_bar.__dict__ if self.breakout_bar else None,
            "retest_bar": self.retest_bar.__dict__ if self.retest_bar else None
        })

    def process_bars(self, bars_1h: List[Bar], bars_4h: List[Bar]):
        if self.state == self.DISABLED:
            return
            
        if len(bars_1h) < 25 or len(bars_4h) < 205:
            return  # Require warmup history broadly before any evaluation
            
        latest_1h = bars_1h[-1]
        latest_4h = bars_4h[-1]
        
        # Detect Bar Gaps precisely limiting systemic structural divergence tracking
        if self.last_1h_ts and (latest_1h.ts_open - self.last_1h_ts) > 3600:
            logger.warning(f"Gap detected on 1h bounds. Defensively isolating. Delta: {latest_1h.ts_open - self.last_1h_ts}s")
            self.state = self.DISABLED
            self._persist()
            return
            
        self.last_1h_ts = latest_1h.ts_open
        self.last_4h_ts = latest_4h.ts_open
        
        # Explicit evaluation branching logic
        if self.state == self.IDLE:
            self._eval_breakout(bars_1h, bars_4h)
        elif self.state == self.WAITING_RETEST:
            self._eval_retest(bars_1h, bars_4h)
        elif self.state == self.RETEST_CONFIRMED:
            self._eval_continuation(bars_1h, bars_4h)
            
        self._persist()

    def _eval_breakout(self, bars_1h: List[Bar], bars_4h: List[Bar]):
        if not is_bullish_regime(bars_4h):
            return
            
        latest = bars_1h[-1]
        past_20 = bars_1h[-21:-1]
        
        highest_20 = max([b.high for b in past_20])
        avg_vol_20 = sum([b.volume for b in past_20]) / 20
        
        rsi = Indicators.calc_rsi([b.close for b in bars_1h])
        
        # Breakout Requirements explicitly mapped identically
        if latest.close <= highest_20: return
        if latest.volume <= 1.25 * avg_vol_20: return
        
        candle_range = latest.high - latest.low
        if candle_range == 0: return
        close_percentile = (latest.close - latest.low) / candle_range
        
        if close_percentile < 0.70: return
        if not (56 <= rsi[-1] <= 74): return
        
        self.state = self.WAITING_RETEST
        self.setup_id = str(uuid.uuid4())
        self.breakout_bar = latest
        self.breakout_level = highest_20
        self.bars_since_breakout = 0
        logger.info(f"Breakout Structure Identified: Close {latest.close} > Highest_20 {highest_20}")

    def _eval_retest(self, bars_1h: List[Bar], bars_4h: List[Bar]):
        if not is_bullish_regime(bars_4h):
            self._reset() 
            return
            
        self.bars_since_breakout += 1
        if self.bars_since_breakout > 5:
            # Retest Timeout explicitly drops back IDLE naturally safely avoiding generic COOLDOWNS structurally
            self._reset()
            return
            
        latest = bars_1h[-1]
        bo_midpoint = (self.breakout_bar.high + self.breakout_bar.low) / 2
        
        touches_zone = latest.low <= self.breakout_level + (self.breakout_bar.high - self.breakout_level) * 0.3
        closes_above = latest.close > self.breakout_level
        closes_above_mid = latest.close > bo_midpoint
        
        if touches_zone and closes_above and closes_above_mid:
            self.state = self.RETEST_CONFIRMED
            self.retest_bar = latest
            logger.info("Retest Successfully Assuring Trailing Midpoint Natively Captured.")

    def _eval_continuation(self, bars_1h: List[Bar], bars_4h: List[Bar]):
        if not is_bullish_regime(bars_4h):
            self._reset()
            return

        latest = bars_1h[-1]
        if latest.ts_open == self.retest_bar.ts_open:
            return 
        
        if latest.close > self.retest_bar.high:
            atr = Indicators.calc_atr(bars_1h, 14)[-1]
            
            # Avoid chasing aggressively past breakout boundaries structurally conservatively
            if (latest.close - self.breakout_level) > (0.8 * atr):
                self._reset() 
                return
                
            self.state = self.SIGNAL_EMITTED
            
            query = """
                INSERT INTO signals (signal_id, symbol, signal_type, regime_snapshot, breakout_level, retest_level, atr, rsi, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id) DO UPDATE SET status=excluded.status
            """
            cur_rsi = Indicators.calc_rsi([b.close for b in bars_1h])[-1]
            db.execute(query, (
                self.setup_id, latest.symbol, "LONG", 
                f"ATR:{atr}_LEVEL:{self.breakout_level}", self.breakout_level, 
                self.retest_bar.low, atr, cur_rsi, "NEW"
            ))
            logger.info(f"Signal {self.setup_id} persisted securely actively.")

    def _reset(self):
        # Exclusively returns IDLE instead of COOLDOWN explicitly
        self.state = self.IDLE
        self.setup_id = None
        self.breakout_bar = None
        self.retest_bar = None
        self.breakout_level = 0.0
        self.bars_since_breakout = 0
