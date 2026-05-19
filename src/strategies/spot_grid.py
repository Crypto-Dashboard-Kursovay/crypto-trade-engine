"""Spot Grid Bot — упрощённая сетка ордеров в диапазоне [price_low, price_high].

Алгоритм:
- N равноотстоящих уровней между low и high.
- На каждой свече определяется "ячейка" — индекс между двумя соседними уровнями.
- При изменении ячейки относительно прошлой генерируется signal:
    * цена упала на K ячеек → накопить K BUY-сигналов
    * цена поднялась на K ячеек → накопить K SELL-сигналов
- Strategy.on_candle отдаёт по одному signal'у за раз (очередь self._pending),
  поэтому при резком движении сигналы выстраиваются в очередь и выливаются
  на последующих свечах.

Только MARKET ордера. Не отслеживает individual fills (нет такого callback'а
в Strategy). При исчерпании баланса ExchangeAdapter вернёт OrderExecutionError —
runner поймает и продолжит.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from domain.enums import Side, TimeFrame
from domain.interfaces import Strategy
from domain.models import Candle, Signal


class SpotGridStrategy(Strategy):
    name = "SpotGridStrategy"

    def __init__(
        self,
        symbol: str,
        timeframe: TimeFrame,
        price_low: Decimal | str,
        price_high: Decimal | str,
        num_levels: int = 10,
        base_per_level: Decimal | str = Decimal("0.001"),
    ) -> None:
        low_d = Decimal(str(price_low))
        high_d = Decimal(str(price_high))
        if low_d <= 0 or high_d <= 0:
            raise ValueError("prices must be > 0")
        if low_d >= high_d:
            raise ValueError(
                f"price_low ({low_d}) must be < price_high ({high_d})"
            )
        if num_levels < 2:
            raise ValueError("num_levels must be >= 2")
        base_d = Decimal(str(base_per_level))
        if base_d <= 0:
            raise ValueError("base_per_level must be > 0")

        self.symbol = symbol
        self.timeframe = timeframe
        self.startup_candle_count = 0
        self._price_low = low_d
        self._price_high = high_d
        self._num_levels = num_levels
        self._base_per_level = base_d
        step = (high_d - low_d) / Decimal(num_levels - 1)
        self._levels: list[Decimal] = [low_d + step * i for i in range(num_levels)]
        self._last_cell: int | None = None
        self._pending: deque[Side] = deque()

    def _find_cell(self, price: Decimal) -> int:
        # 0 — ниже самого низкого уровня, num_levels-1 — выше самого высокого
        if price <= self._levels[0]:
            return 0
        if price >= self._levels[-1]:
            return self._num_levels - 1
        for i in range(self._num_levels - 1):
            if self._levels[i] <= price < self._levels[i + 1]:
                return i
        return self._num_levels - 1  # unreachable

    def on_candle(self, candle: Candle) -> Signal | None:
        cell = self._find_cell(candle.close)
        if self._last_cell is None:
            self._last_cell = cell
            return self._emit_pending(candle)

        diff = cell - self._last_cell
        if diff < 0:
            for _ in range(-diff):
                self._pending.append(Side.BUY)
        elif diff > 0:
            for _ in range(diff):
                self._pending.append(Side.SELL)
        self._last_cell = cell

        return self._emit_pending(candle)

    def _emit_pending(self, candle: Candle) -> Signal | None:
        if not self._pending:
            return None
        side = self._pending.popleft()
        return Signal(
            strategy_name=self.name,
            symbol=self.symbol,
            side=side,
            size=self._base_per_level,
            price=candle.close,
            metadata={"cell": str(self._last_cell)},
        )


__all__ = ["SpotGridStrategy"]
