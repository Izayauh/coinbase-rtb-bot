import logging
from typing import List
from .models import Bar

logger = logging.getLogger(__name__)

class Indicators:
    @staticmethod
    def calc_ema(prices: List[float], period: int) -> List[float]:
        if len(prices) < period:
            return [None] * len(prices)
        
        ema = [None] * len(prices)
        current_ema = sum(prices[:period]) / period
        ema[period - 1] = current_ema
        
        multiplier = 2 / (period + 1)
        for i in range(period, len(prices)):
            current_ema = (prices[i] - current_ema) * multiplier + current_ema
            ema[i] = current_ema
            
        return ema

    @staticmethod
    def calc_atr(bars: List[Bar], period: int) -> List[float]:
        if len(bars) == 0:
            return []
            
        trs = [0.0] * len(bars)
        for i in range(1, len(bars)):
            trs[i] = max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - bars[i-1].close),
                abs(bars[i].low - bars[i-1].close)
            )
            
        atr = [None] * len(bars)
        if len(bars) <= period:
            return atr
            
        current_atr = sum(trs[1:period+1]) / period
        atr[period] = current_atr
        
        for i in range(period+1, len(bars)):
            current_atr = (current_atr * (period - 1) + trs[i]) / period
            atr[i] = current_atr

        return atr

    @staticmethod
    def calc_rsi(closes: List[float], period: int=14) -> List[float]:
        if len(closes) <= period:
            return [None] * len(closes)
        
        gains = [0.0] * len(closes)
        losses = [0.0] * len(closes)
        for i in range(1, len(closes)):
            change = closes[i] - closes[i-1]
            gains[i] = change if change > 0 else 0
            losses[i] = abs(change) if change < 0 else 0
            
        rsi = [None] * len(closes)
        
        avg_gain = sum(gains[1:period+1]) / period
        avg_loss = sum(losses[1:period+1]) / period
        
        rsi[period] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
            
        for i in range(period+1, len(closes)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            
            rsi[i] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
                
        return rsi

def is_bullish_regime(bars_4h: List[Bar], atr_threshold=0.005) -> bool:
    if len(bars_4h) < 201:
        return False
        
    closes = [b.close for b in bars_4h]
    ema_50 = Indicators.calc_ema(closes, 50)
    ema_200 = Indicators.calc_ema(closes, 200)
    atr = Indicators.calc_atr(bars_4h, 14)
    
    if ema_50[-1] is None or ema_200[-1] is None or atr[-1] is None:
        return False
        
    current_close = closes[-1]
    
    close_above_200 = current_close > ema_200[-1]
    ema_50_above_200 = ema_50[-1] > ema_200[-1]
    slope_200_positive = ema_200[-1] > ema_200[-2]
    high_vol = (atr[-1] / current_close) > atr_threshold
    
    return close_above_200 and ema_50_above_200 and slope_200_positive and high_vol
