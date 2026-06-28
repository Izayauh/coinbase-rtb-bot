"""
research/costs.py — Friction model for Lane A research harness.

Defines entry and exit costs separately per the binding spec:
    ENTRY COST = taker_fee + half_spread + slippage
    EXIT COST  = taker_fee + half_spread + slippage
    ROUND-TRIP = ENTRY + EXIT

Sensitivity: multiply all components by a uniform scalar (0.5×, 1.0×, 1.5×).
Per-asset spread override is supported (altcoins have wider spreads).
"""
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class FrictionModel:
    """
    All values in basis points (1 bp = 0.01%).

    Default: Coinbase Advanced Trade taker tier.
    """
    taker_fee_bps: float = 8.0       # Coinbase taker fee
    half_spread_bps: float = 1.5     # half of bid-ask spread
    slippage_bps: float = 3.0        # market order slippage estimate

    sensitivity: float = 1.0         # uniform multiplier (0.5, 1.0, 1.5)

    # Per-asset spread overrides (symbol → half_spread_bps)
    # If a symbol is not in here, the default half_spread_bps is used.
    spread_overrides: Dict[str, float] = field(default_factory=dict)

    def _half_spread(self, symbol: Optional[str] = None) -> float:
        if symbol and symbol in self.spread_overrides:
            return self.spread_overrides[symbol] * self.sensitivity
        return self.half_spread_bps * self.sensitivity

    def _fee(self) -> float:
        return self.taker_fee_bps * self.sensitivity

    def _slip(self) -> float:
        return self.slippage_bps * self.sensitivity

    def one_way_bps(self, symbol: Optional[str] = None) -> float:
        """Total one-way cost in bps."""
        return self._fee() + self._half_spread(symbol) + self._slip()

    def round_trip_bps(self, symbol: Optional[str] = None) -> float:
        """Total round-trip cost in bps."""
        return 2 * self.one_way_bps(symbol)

    def apply_entry_cost(self, price: float, direction: str,
                         symbol: Optional[str] = None) -> float:
        """
        Return the effective fill price after entry friction.

        Long entry: pay more (price goes up).
        Short entry: receive less (price goes down).
        """
        cost_frac = self.one_way_bps(symbol) / 10_000
        if direction == "long":
            return price * (1 + cost_frac)
        else:  # short
            return price * (1 - cost_frac)

    def apply_exit_cost(self, price: float, direction: str,
                        symbol: Optional[str] = None) -> float:
        """
        Return the effective fill price after exit friction.

        Long exit (sell): receive less.
        Short exit (cover): pay more.
        """
        cost_frac = self.one_way_bps(symbol) / 10_000
        if direction == "long":
            return price * (1 - cost_frac)
        else:  # short
            return price * (1 + cost_frac)

    def entry_cost_dollars(self, price: float, size: float,
                           symbol: Optional[str] = None) -> float:
        """Dollar cost of entry friction for given price × size."""
        return price * size * self.one_way_bps(symbol) / 10_000

    def exit_cost_dollars(self, price: float, size: float,
                          symbol: Optional[str] = None) -> float:
        """Dollar cost of exit friction for given price × size."""
        return price * size * self.one_way_bps(symbol) / 10_000

    def summary(self, symbol: Optional[str] = None) -> str:
        ow = self.one_way_bps(symbol)
        rt = self.round_trip_bps(symbol)
        return (f"Friction({self.sensitivity:.1f}x): "
                f"fee={self._fee():.1f} spread={self._half_spread(symbol):.1f} "
                f"slip={self._slip():.1f} → {ow:.1f} bps one-way, {rt:.1f} bps RT")


# Pre-built configurations for sensitivity runs
FRICTION_HALF = FrictionModel(sensitivity=0.5)
FRICTION_BASE = FrictionModel(sensitivity=1.0)
FRICTION_HIGH = FrictionModel(sensitivity=1.5)

# Altcoin spread overrides (wider markets)
ALTCOIN_SPREADS = {
    "DOGE-USD": 3.0,
    "SHIB-USD": 5.0,
    "AVAX-USD": 2.5,
    "DOT-USD": 2.5,
    "LINK-USD": 2.0,
    "ADA-USD": 2.5,
    "MATIC-USD": 2.5,
    "XRP-USD": 2.0,
    "SOL-USD": 1.5,
    # BTC-USD and ETH-USD use defaults (tight spreads)
}
