"""
risk_manager.py - Automated Take-Profit / Stop-Loss logic for TradeMatrix.

Handles LONG positions (buy first, exit with a SELL). It does no I/O and has
no Kivy dependency, so it can be called from the UI thread, a worker thread,
or unit tests.
"""
from typing import Dict


class RiskManager:
    """Calculates TP/SL levels and checks live prices against them."""

    PRICE_DECIMALS = 8  # enough precision for low-priced crypto assets

    def __init__(self, target_profit_pct: float = 2.0, stop_loss_pct: float = 1.5):
        """
        :param target_profit_pct: take-profit distance above entry, in percent (2.0 = +2%).
        :param stop_loss_pct: stop-loss distance below entry, in percent (1.5 = -1.5%).
        """
        self.target_profit_pct = self._validate_pct("target_profit_pct", target_profit_pct)
        self.stop_loss_pct = self._validate_pct("stop_loss_pct", stop_loss_pct, upper=100.0)

    # ------------------------------------------------------------ validation
    @staticmethod
    def _validate_pct(name: str, value: float, upper: float = float("inf")) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number, got {value!r}")
        if not 0 < value < upper or value != value:  # value != value rejects NaN
            raise ValueError(f"{name} must be greater than 0 (and below {upper}), got {value}")
        return value

    @staticmethod
    def _validate_price(name: str, value: float) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number, got {value!r}")
        if value <= 0 or value != value or value == float("inf"):
            raise ValueError(f"{name} must be a positive, finite number, got {value}")
        return value

    # ------------------------------------------------------------ levels
    def calculate_levels(self, entry_price: float) -> Dict[str, float]:
        """Return the exact take-profit and stop-loss prices for an entry price."""
        entry = self._validate_price("entry_price", entry_price)
        target = entry * (1 + self.target_profit_pct / 100.0)
        stop = entry * (1 - self.stop_loss_pct / 100.0)
        return {
            "entry_price": round(entry, self.PRICE_DECIMALS),
            "target_price": round(target, self.PRICE_DECIMALS),
            "stop_price": round(stop, self.PRICE_DECIMALS),
        }

    # ------------------------------------------------------------ evaluation
    def evaluate_trade(self, entry_price: float, current_price: float) -> Dict[str, object]:
        """
        Compare the live price with the TP/SL levels of a long position.

        Returns a dict with:
            status        "TRIGGER_EXIT" or "HOLD"
            action        "SELL" on exit, otherwise None
            reason        "TAKE_PROFIT" / "STOP_LOSS" / "WITHIN_RANGE"
            message       human-readable text for the UI
            entry_price, current_price, target_price, stop_price, pnl_pct
        """
        levels = self.calculate_levels(entry_price)
        current = self._validate_price("current_price", current_price)
        entry = levels["entry_price"]
        pnl_pct = round((current - entry) / entry * 100.0, 4)

        result = {
            **levels,
            "current_price": round(current, self.PRICE_DECIMALS),
            "pnl_pct": pnl_pct,
        }

        if current >= levels["target_price"]:
            result.update(
                status="TRIGGER_EXIT", action="SELL", reason="TAKE_PROFIT",
                message=f"Take-profit hit: {current} >= {levels['target_price']} ({pnl_pct:+.2f}%)",
            )
        elif current <= levels["stop_price"]:
            result.update(
                status="TRIGGER_EXIT", action="SELL", reason="STOP_LOSS",
                message=f"Stop-loss hit: {current} <= {levels['stop_price']} ({pnl_pct:+.2f}%)",
            )
        else:
            result.update(
                status="HOLD", action=None, reason="WITHIN_RANGE",
                message=f"Holding: {levels['stop_price']} < {current} < {levels['target_price']} "
                        f"({pnl_pct:+.2f}%)",
            )
        return result
