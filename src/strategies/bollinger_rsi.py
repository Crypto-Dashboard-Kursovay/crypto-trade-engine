"""Bollinger Bands + RSI confirmation. Двойной фильтр: вход только при совпадении.

- BUY: close <= BB.lower И RSI < oversold
- SELL: close >= BB.upper И RSI > overbought

Снижает количество ложных сигналов классического BB-only.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from domain.enums import Side, TimeFrame
from domain.interfaces import Strategy
from domain.models import Candle, Signal

from .bollinger_bands import _stddev


class BollingerRsi(Strategy):
    name = "BollingerRsi"

    def __init__(
        self,
        symbol: str,
        timeframe: TimeFrame,
        bb_period: int = 20,
        bb_std: Decimal | str = Decimal("2.0"),
        rsi_period: int = 14,
        oversold: Decimal | str = Decimal("30"),
        overbought: Decimal | str = Decimal("70"),
        order_size: Decimal | str = Decimal("0.001"),
    ) -> None:
        if bb_period <= 1 or rsi_period <= 1:
            raise ValueError("periods must be > 1")
        bb_std_d = Decimal(str(bb_std))
        oversold_d = Decimal(str(oversold))
        overbought_d = Decimal(str(overbought))
        if bb_std_d <= 0:
            raise ValueError("bb_std must be > 0")
        if not (Decimal("0") < oversold_d < overbought_d < Decimal("100")):
            raise ValueError("need 0 < oversold < overbought < 100")
        order_size_d = Decimal(str(order_size))
        if order_size_d <= 0:
            raise ValueError("order_size must be > 0")

        self.symbol = symbol
        self.timeframe = timeframe
        self.startup_candle_count = max(bb_period, rsi_period + 1)
        self._bb_period = bb_period
        self._bb_std = bb_std_d
        self._rsi_period = rsi_period
        self._oversold = oversold_d
        self._overbought = overbought_d
        self._order_size = order_size_d

        self._closes: deque[Decimal] = deque(maxlen=max(bb_period, rsi_period + 1))
        self._avg_gain: Decimal | None = None
        self._avg_loss: Decimal | None = None
        self._in_position = False

    def _update_rsi(self) -> Decimal | None:
        closes_list = list(self._closes)
        if len(closes_list) < self._rsi_period + 1:
            return None
        if self._avg_gain is None or self._avg_loss is None:
            gains = Decimal("0")
            losses = Decimal("0")
            window = closes_list[-(self._rsi_period + 1):]
            for i in range(1, len(window)):
                diff = window[i] - window[i - 1]
                if diff >= 0:
                    gains += diff
                else:
                    losses += -diff
            self._avg_gain = gains / Decimal(self._rsi_period)
            self._avg_loss = losses / Decimal(self._rsi_period)
        else:
            diff = closes_list[-1] - closes_list[-2]
            current_gain = diff if diff > 0 else Decimal("0")
            current_loss = -diff if diff < 0 else Decimal("0")
            n = Decimal(self._rsi_period)
            self._avg_gain = (self._avg_gain * (n - 1) + current_gain) / n
            self._avg_loss = (self._avg_loss * (n - 1) + current_loss) / n
        if self._avg_loss == 0:
            return Decimal("100")
        rs = self._avg_gain / self._avg_loss
        return Decimal("100") - Decimal("100") / (Decimal("1") + rs)

    def on_candle(self, candle: Candle) -> Signal | None:
        self._closes.append(candle.close)
        rsi = self._update_rsi()
        if rsi is None or len(self._closes) < self._bb_period:
            return None

        bb_window = list(self._closes)[-self._bb_period:]
        mean = sum(bb_window) / Decimal(self._bb_period)
        std = _stddev(bb_window, mean)
        lower = mean - self._bb_std * std
        upper = mean + self._bb_std * std

        if candle.close <= lower and rsi < self._oversold and not self._in_position:
            self._in_position = True
            return Signal(
                strategy_name=self.name,
                symbol=self.symbol,
                side=Side.BUY,
                size=self._order_size,
                price=candle.close,
                metadata={"rsi": str(rsi), "lower": str(lower), "upper": str(upper)},
            )
        if candle.close >= upper and rsi > self._overbought and self._in_position:
            self._in_position = False
            return Signal(
                strategy_name=self.name,
                symbol=self.symbol,
                side=Side.SELL,
                size=self._order_size,
                price=candle.close,
                metadata={"rsi": str(rsi), "lower": str(lower), "upper": str(upper)},
            )
        return None


__all__ = ["BollingerRsi"]
