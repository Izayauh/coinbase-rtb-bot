import logging

logger = logging.getLogger(__name__)

class RiskManager:
    # 0.20% risk model defined strictly
    MAX_RISK_PERCENT = 0.002 
    # Bounded entry model: reject entries structurally if slippage exceeds 0.50% from signal limit
    MAX_SLIPPAGE = 0.005 

    @staticmethod
    def calculate_size(portfolio_value: float, entry_price: float, stop_loss: float) -> float:
        """
        Computes absolute order size protecting exclusively 0.20% of net portfolio 
        against the exact mathematical distance between entry and the structural stop loss.
        """
        if portfolio_value <= 0 or entry_price <= 0 or stop_loss <= 0:
            logger.error("Invalid negative/zero parameters passed to risk context. Rejecting.")
            return 0.0

        if stop_loss >= entry_price:
            logger.error(f"Stop loss ({stop_loss}) >= Entry ({entry_price}). Rejecting constraint.")
            return 0.0

        dollars_at_risk = portfolio_value * RiskManager.MAX_RISK_PERCENT
        per_unit_risk = entry_price - stop_loss
        
        size = dollars_at_risk / per_unit_risk
        
        # Rounding explicitly for standard crypto base limits - dynamic ticks could be added later
        return round(size, 5)

    @staticmethod
    def get_ioc_limit(signal_price: float) -> float:
        """
        Generates strict upper threshold for IOC limit orders bounding slippage safely.
        """
        return round(signal_price * (1 + RiskManager.MAX_SLIPPAGE), 2)
