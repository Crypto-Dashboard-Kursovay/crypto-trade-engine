"""DCA (Dollar Cost Averaging) — простейший накопительный бот.

Покупает фиксированную сумму quote-валюты каждые `interval_candles` свечей.
Не закрывает позицию (стратегия накопления).
"""

from __future__ import annotations

from decimal import Decimal

from domain.enums import Side, TimeFrame
from domain.interfaces import Strategy
from domain.models import Candle, Signal


class DcaStrategy(Strategy):
    name = "DcaStrategy"

    def __init__(
        self,
        symbol: str,
        timeframe: TimeFrame,
        buy_amount_quote: Decimal | str = Decimal("10"),
        interval_candles: int = 24,
    ) -> None:
        buy_amount_d = Decimal(str(buy_amount_quote))
        if buy_amount_d <= 0:
            raise ValueError("buy_amount_quote must be > 0")
        if interval_candles <= 0:
            raise ValueError("interval_candles must be > 0")

        self.symbol = symbol
        self.timeframe = timeframe
        self.startup_candle_count = 0
        self._buy_amount = buy_amount_d
        self._interval = interval_candles
        self._counter = 0

    def on_candle(self, candle: Candle) -> Signal | None:
        self._counter += 1
        if self._counter < self._interval:
            return None
        self._counter = 0
        if candle.close <= 0:
            return None
        size = self._buy_amount / candle.close
        return Signal(
            strategy_name=self.name,
            symbol=self.symbol,
            side=Side.BUY,
            size=size,
            price=candle.close,
            metadata={"quote_amount": str(self._buy_amount)},
        )


__all__ = ["DcaStrategy"]
