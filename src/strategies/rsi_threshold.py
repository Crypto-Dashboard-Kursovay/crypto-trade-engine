"""RSI threshold strategy. Mean-reversion: BUY на oversold, SELL на overbought.

Wilder's RSI: первая средняя — простая, дальше EMA-like с alpha=1/period.
Состояние: только текущие avg_gain / avg_loss и флаг "в позиции" (упрощение —
без частичных выходов).
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from domain.enums import Side, TimeFrame
from domain.interfaces import Strategy
from domain.models import Candle, Signal


class RsiThreshold(Strategy):
    name = "RsiThreshold"

    def __init__(
        self,
        symbol: str,
        timeframe: TimeFrame,
        rsi_period: int = 14,
        oversold: Decimal | str = Decimal("30"),
        overbought: Decimal | str = Decimal("70"),
        order_size: Decimal | str = Decimal("0.001"),
    ) -> None:
        if rsi_period <= 1:
            raise ValueError("rsi_period must be > 1")
        oversold_d = Decimal(str(oversold))
        overbought_d = Decimal(str(overbought))
        if not (Decimal("0") < oversold_d < overbought_d < Decimal("100")):
            raise ValueError(
                f"need 0 < oversold ({oversold_d}) < overbought ({overbought_d}) < 100"
            )
        order_size_d = Decimal(str(order_size))
        if order_size_d <= 0:
            raise ValueError("order_size must be > 0")

        self.symbol = symbol
        self.timeframe = timeframe
        self.startup_candle_count = rsi_period + 1
        self._rsi_period = rsi_period
        self._oversold = oversold_d
        self._overbought = overbought_d
        self._order_size = order_size_d

        self._closes: deque[Decimal] = deque(maxlen=rsi_period + 1)
        self._avg_gain: Decimal | None = None
        self._avg_loss: Decimal | None = None
        self._in_position = False

    def on_candle(self, candle: Candle) -> Signal | None:
        self._closes.append(candle.close)
        if len(self._closes) < self._rsi_period + 1:
            return None

        if self._avg_gain is None or self._avg_loss is None:
            # Первая инициализация — простая средняя прироста/убыли по N последним свечам
            gains = Decimal("0")
            losses = Decimal("0")
            closes = list(self._closes)
            for i in range(1, len(closes)):
                diff = closes[i] - closes[i - 1]
                if diff >= 0:
                    gains += diff
                else:
                    losses += -diff
            self._avg_gain = gains / Decimal(self._rsi_period)
            self._avg_loss = losses / Decimal(self._rsi_period)
        else:
            # Wilder smoothing: новое значение = (prev*(N-1) + current)/N
            closes_list = list(self._closes)
            diff = closes_list[-1] - closes_list[-2]
            current_gain = diff if diff > 0 else Decimal("0")
            current_loss = -diff if diff < 0 else Decimal("0")
            n = Decimal(self._rsi_period)
            self._avg_gain = (self._avg_gain * (n - 1) + current_gain) / n
            self._avg_loss = (self._avg_loss * (n - 1) + current_loss) / n

        if self._avg_loss == 0:
            rsi = Decimal("100")
        else:
            rs = self._avg_gain / self._avg_loss
            rsi = Decimal("100") - Decimal("100") / (Decimal("1") + rs)

        if rsi < self._oversold and not self._in_position:
            self._in_position = True
            return Signal(
                strategy_name=self.name,
                symbol=self.symbol,
                side=Side.BUY,
                size=self._order_size,
                price=candle.close,
                metadata={"rsi": str(rsi)},
            )
        if rsi > self._overbought and self._in_position:
            self._in_position = False
            return Signal(
                strategy_name=self.name,
                symbol=self.symbol,
                side=Side.SELL,
                size=self._order_size,
                price=candle.close,
                metadata={"rsi": str(rsi)},
            )
        return None


__all__ = ["RsiThreshold"]
