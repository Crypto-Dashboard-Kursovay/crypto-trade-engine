"""MACD Cross strategy. Сигналы по пересечению MACD-линии и сигнальной EMA.

MACD = EMA(fast) - EMA(slow). Signal = EMA(MACD, signal_period).
- MACD пересекает signal снизу вверх → BUY
- MACD пересекает signal сверху вниз → SELL
"""

from __future__ import annotations

from decimal import Decimal

from domain.enums import Side, TimeFrame
from domain.interfaces import Strategy
from domain.models import Candle, Signal


def _ema_alpha(period: int) -> Decimal:
    return Decimal("2") / (Decimal(period) + Decimal("1"))


class MacdCross(Strategy):
    name = "MacdCross"

    def __init__(
        self,
        symbol: str,
        timeframe: TimeFrame,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
        order_size: Decimal | str = Decimal("0.001"),
    ) -> None:
        if fast_period <= 0 or slow_period <= 0 or signal_period <= 0:
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
        # Грубая оценка: для устаканивания EMA нужно ~3-4 раза по slowest period
        self.startup_candle_count = slow_period + signal_period
        self._fast_period = fast_period
        self._slow_period = slow_period
        self._signal_period = signal_period
        self._order_size = order_size_d

        self._fast_alpha = _ema_alpha(fast_period)
        self._slow_alpha = _ema_alpha(slow_period)
        self._signal_alpha = _ema_alpha(signal_period)

        self._ema_fast: Decimal | None = None
        self._ema_slow: Decimal | None = None
        self._ema_signal: Decimal | None = None
        self._prev_macd: Decimal | None = None
        self._prev_signal: Decimal | None = None

    @staticmethod
    def _next_ema(prev: Decimal | None, value: Decimal, alpha: Decimal) -> Decimal:
        if prev is None:
            return value
        return alpha * value + (Decimal("1") - alpha) * prev

    def on_candle(self, candle: Candle) -> Signal | None:
        self._ema_fast = self._next_ema(self._ema_fast, candle.close, self._fast_alpha)
        self._ema_slow = self._next_ema(self._ema_slow, candle.close, self._slow_alpha)
        macd = self._ema_fast - self._ema_slow
        self._ema_signal = self._next_ema(self._ema_signal, macd, self._signal_alpha)
        signal_line = self._ema_signal

        result: Signal | None = None
        if self._prev_macd is not None and self._prev_signal is not None:
            crossed_up = self._prev_macd <= self._prev_signal and macd > signal_line
            crossed_down = self._prev_macd >= self._prev_signal and macd < signal_line
            if crossed_up:
                result = Signal(
                    strategy_name=self.name,
                    symbol=self.symbol,
                    side=Side.BUY,
                    size=self._order_size,
                    price=candle.close,
                    metadata={"macd": str(macd), "signal": str(signal_line)},
                )
            elif crossed_down:
                result = Signal(
                    strategy_name=self.name,
                    symbol=self.symbol,
                    side=Side.SELL,
                    size=self._order_size,
                    price=candle.close,
                    metadata={"macd": str(macd), "signal": str(signal_line)},
                )

        self._prev_macd = macd
        self._prev_signal = signal_line
        return result


__all__ = ["MacdCross"]
