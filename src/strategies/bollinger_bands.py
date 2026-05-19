"""Bollinger Bands strategy. Mean-reversion: BUY на касании нижней полосы, SELL — верхней.

middle = SMA(period), std = stddev(period). lower = middle - N*std, upper = middle + N*std.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from domain.enums import Side, TimeFrame
from domain.interfaces import Strategy
from domain.models import Candle, Signal


def _stddev(values: list[Decimal], mean: Decimal) -> Decimal:
    n = Decimal(len(values))
    var = sum((v - mean) ** 2 for v in values) / n
    # Decimal не имеет sqrt в base, через .sqrt() в context
    return var.sqrt() if hasattr(var, "sqrt") else Decimal(str(float(var) ** 0.5))


class BollingerBands(Strategy):
    name = "BollingerBands"

    def __init__(
        self,
        symbol: str,
        timeframe: TimeFrame,
        period: int = 20,
        num_std: Decimal | str = Decimal("2.0"),
        order_size: Decimal | str = Decimal("0.001"),
    ) -> None:
        if period <= 1:
            raise ValueError("period must be > 1")
        num_std_d = Decimal(str(num_std))
        if num_std_d <= 0:
            raise ValueError("num_std must be > 0")
        order_size_d = Decimal(str(order_size))
        if order_size_d <= 0:
            raise ValueError("order_size must be > 0")

        self.symbol = symbol
        self.timeframe = timeframe
        self.startup_candle_count = period
        self._period = period
        self._num_std = num_std_d
        self._order_size = order_size_d

        self._closes: deque[Decimal] = deque(maxlen=period)
        self._in_position = False

    def on_candle(self, candle: Candle) -> Signal | None:
        self._closes.append(candle.close)
        if len(self._closes) < self._period:
            return None

        closes_list = list(self._closes)
        mean = sum(closes_list) / Decimal(self._period)
        std = _stddev(closes_list, mean)
        lower = mean - self._num_std * std
        upper = mean + self._num_std * std

        if candle.close <= lower and not self._in_position:
            self._in_position = True
            return Signal(
                strategy_name=self.name,
                symbol=self.symbol,
                side=Side.BUY,
                size=self._order_size,
                price=candle.close,
                metadata={"lower": str(lower), "upper": str(upper)},
            )
        if candle.close >= upper and self._in_position:
            self._in_position = False
            return Signal(
                strategy_name=self.name,
                symbol=self.symbol,
                side=Side.SELL,
                size=self._order_size,
                price=candle.close,
                metadata={"lower": str(lower), "upper": str(upper)},
            )
        return None


__all__ = ["BollingerBands"]
