"""SMA Cross strategy. Базовый пример для Phase 1.

Логика: пересечение быстрой и медленной скользящих средних по close-ценам.
- fast пересекает slow снизу вверх → BUY
- fast пересекает slow сверху вниз → SELL
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from domain.enums import Side, TimeFrame
from domain.interfaces import Strategy
from domain.models import Candle, Signal


class SmaCross(Strategy):
    name = "SmaCross"

    def __init__(
        self,
        symbol: str,
        timeframe: TimeFrame,
        fast_period: int = 50,
        slow_period: int = 200,
        order_size: Decimal | str | int | float = Decimal("0.001"),
    ) -> None:
        if fast_period <= 0 or slow_period <= 0:
            raise ValueError("periods must be positive")
        if fast_period >= slow_period:
            raise ValueError(
                f"fast_period ({fast_period}) must be < slow_period ({slow_period})"
            )
        order_size_d = Decimal(str(order_size))
        if order_size_d <= 0:
            raise ValueError("order_size must be > 0")

        self.symbol = symbol
        self.timeframe = timeframe
        self.startup_candle_count = slow_period
        self._fast_period = fast_period
        self._slow_period = slow_period
        self._order_size = order_size_d
        self._closes: deque[Decimal] = deque(maxlen=slow_period)
        self._prev_fast: Decimal | None = None
        self._prev_slow: Decimal | None = None

    def on_candle(self, candle: Candle) -> Signal | None:
        self._closes.append(candle.close)
        if len(self._closes) < self._slow_period:
            return None

        as_list = list(self._closes)
        fast = sum(as_list[-self._fast_period :]) / Decimal(self._fast_period)
        slow = sum(as_list) / Decimal(self._slow_period)

        signal: Signal | None = None
        if self._prev_fast is not None and self._prev_slow is not None:
            crossed_up = self._prev_fast <= self._prev_slow and fast > slow
            crossed_down = self._prev_fast >= self._prev_slow and fast < slow
            if crossed_up:
                signal = Signal(
                    strategy_name=self.name,
                    symbol=self.symbol,
                    side=Side.BUY,
                    size=self._order_size,
                    price=candle.close,
                )
            elif crossed_down:
                signal = Signal(
                    strategy_name=self.name,
                    symbol=self.symbol,
                    side=Side.SELL,
                    size=self._order_size,
                    price=candle.close,
                )

        self._prev_fast = fast
        self._prev_slow = slow
        return signal


__all__ = ["SmaCross"]
