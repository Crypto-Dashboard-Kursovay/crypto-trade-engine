"""Adaptive RSI strategy. Two-phase: calibrate, then trade.

Phase 1 (warmup): collects `optimization_candles` of closes. No signals.
Phase 2 (on_start): grid-search over (rsi_period, oversold, overbought) combos,
  simulates virtual trades over warmup data, picks max pseudo-Sharpe.
Phase 3 (trading): uses calibrated parameters for live RSI signals.

Works identically in live and backtest (uses same warmup → calibration path).
"""

from __future__ import annotations

import random
from collections import deque
from decimal import Decimal

from domain.enums import Side, TimeFrame
from domain.interfaces import Strategy
from domain.models import Candle, Signal


class AdaptiveRsiStrategy(Strategy):
    name = "AdaptiveRsi"

    def __init__(
        self,
        symbol: str,
        timeframe: TimeFrame,
        optimization_candles: int = 500,
        rsi_period_range: list[int] | None = None,
        oversold_range: list[int] | None = None,
        overbought_range: list[int] | None = None,
        order_size: str | Decimal = "0.001",
    ) -> None:
        optimization_candles = int(str(optimization_candles))
        order_size_d = Decimal(str(order_size))
        if order_size_d <= 0:
            raise ValueError("order_size must be > 0")
        if optimization_candles < 50:
            raise ValueError("optimization_candles must be >= 50")

        self.symbol = symbol
        self.timeframe = timeframe
        self.startup_candle_count = optimization_candles
        self._order_size = order_size_d

        self._periods = rsi_period_range or [7, 10, 14, 20, 25, 30, 35, 40, 50]
        self._oversolds = oversold_range or [20, 25, 30, 35, 40]
        self._overboughts = overbought_range or [60, 65, 70, 75, 80]

        # Warmup: collect closes without trading
        self._closes: deque[Decimal] = deque(maxlen=optimization_candles)
        self._calibrated = False

        # After calibration, hold best params
        self._best_rsi_period: int | None = None
        self._best_oversold: Decimal | None = None
        self._best_overbought: Decimal | None = None

        # RSI state (used in trading phase)
        self._rsi_closes: deque[Decimal] = deque(maxlen=self._periods[-1] + 1)
        self._avg_gain: Decimal | None = None
        self._avg_loss: Decimal | None = None
        self._in_position = False

    async def on_start(self) -> None:
        """Run grid-search calibration over collected warmup closes."""
        if self._calibrated:
            return

        closes = list(self._closes)
        if len(closes) < 50:
            # Not enough data for meaningful calibration — use defaults
            self._best_rsi_period = 14
            self._best_oversold = Decimal("30")
            self._best_overbought = Decimal("70")
        else:
            best_sharpe = Decimal("-Infinity")
            best_params: list[tuple[int, int, int, Decimal]] = []

            for rsi_period in self._periods:
                if rsi_period > len(closes):
                    continue
                for oversold in self._oversolds:
                    for overbought in self._overboughts:
                        if oversold >= overbought:
                            continue
                        sharpe = _evaluate_rsi_params(
                            closes, rsi_period, oversold, overbought
                        )
                        if sharpe > best_sharpe:
                            best_sharpe = sharpe
                            best_params = [(rsi_period, oversold, overbought, sharpe)]
                        elif sharpe == best_sharpe:
                            best_params.append((rsi_period, oversold, overbought, sharpe))

            # Tie-break with jitter: случайный выбор среди лучших.
            # Это даёт разные параметры при рестарте, если Sharpe одинаковый.
            chosen = random.choice(best_params)
            self._best_rsi_period = chosen[0]
            self._best_oversold = Decimal(str(chosen[1]))
            self._best_overbought = Decimal(str(chosen[2]))

        self._rsi_closes = deque(maxlen=self._best_rsi_period + 1)
        self._calibrated = True

    def on_candle(self, candle: Candle) -> Signal | None:
        if not self._calibrated:
            # Warmup phase — just collect
            self._closes.append(candle.close)
            return None

        # --- Trading phase ---
        assert self._best_rsi_period is not None

        self._rsi_closes.append(candle.close)
        if len(self._rsi_closes) < self._best_rsi_period + 1:
            return None

        # Wilder's RSI
        rsi_period = self._best_rsi_period
        if self._avg_gain is None or self._avg_loss is None:
            # SMA initialisation
            gains = Decimal("0")
            losses = Decimal("0")
            cl = list(self._rsi_closes)
            for i in range(1, len(cl)):
                diff = cl[i] - cl[i - 1]
                if diff >= 0:
                    gains += diff
                else:
                    losses += -diff
            self._avg_gain = gains / Decimal(rsi_period)
            self._avg_loss = losses / Decimal(rsi_period)
        else:
            cl = list(self._rsi_closes)
            diff = cl[-1] - cl[-2]
            current_gain = diff if diff > 0 else Decimal("0")
            current_loss = -diff if diff < 0 else Decimal("0")
            n = Decimal(rsi_period)
            self._avg_gain = (self._avg_gain * (n - 1) + current_gain) / n
            self._avg_loss = (self._avg_loss * (n - 1) + current_loss) / n

        if self._avg_loss == 0:
            rsi = Decimal("100")
        else:
            rs = self._avg_gain / self._avg_loss
            rsi = Decimal("100") - Decimal("100") / (Decimal("1") + rs)

        assert self._best_oversold is not None
        assert self._best_overbought is not None

        if rsi < self._best_oversold and not self._in_position:
            self._in_position = True
            return Signal(
                strategy_name=self.name,
                symbol=self.symbol,
                side=Side.BUY,
                size=self._order_size,
                price=candle.close,
                metadata={
                    "rsi": str(rsi),
                    "rsi_period": str(self._best_rsi_period),
                },
            )
        if rsi > self._best_overbought and self._in_position:
            self._in_position = False
            return Signal(
                strategy_name=self.name,
                symbol=self.symbol,
                side=Side.SELL,
                size=self._order_size,
                price=candle.close,
                metadata={
                    "rsi": str(rsi),
                    "rsi_period": str(self._best_rsi_period),
                },
            )
        return None


def _simulate_rsi_trades(
    closes: list[Decimal],
    rsi_period: int,
    oversold: int,
    overbought: int,
) -> list[Decimal]:
    """Run RSI over `closes` and return list of PnL per simulated trade.

    BUY when RSI drops below `oversold` (and not in position).
    SELL when RSI rises above `overbought` (and in position).
    PnL = sell_close - buy_close (simplified, no fees).
    """
    pnls: list[Decimal] = []
    in_position = False
    buy_price: Decimal | None = None

    avg_gain: Decimal | None = None
    avg_loss: Decimal | None = None

    # We need rsi_period + 1 closes to start computing RSI.
    for i in range(rsi_period, len(closes)):
        if avg_gain is None or avg_loss is None:
            gains = Decimal("0")
            losses = Decimal("0")
            # Use the first (rsi_period + 1) closes for initialization
            window = closes[i - rsi_period : i + 1]
            for j in range(1, len(window)):
                diff = window[j] - window[j - 1]
                if diff >= 0:
                    gains += diff
                else:
                    losses += -diff
            avg_gain = gains / Decimal(rsi_period)
            avg_loss = losses / Decimal(rsi_period)
        else:
            diff = closes[i] - closes[i - 1]
            current_gain = diff if diff > 0 else Decimal("0")
            current_loss = -diff if diff < 0 else Decimal("0")
            n = Decimal(rsi_period)
            avg_gain = (avg_gain * (n - 1) + current_gain) / n
            avg_loss = (avg_loss * (n - 1) + current_loss) / n

        if avg_loss == 0:
            rsi = Decimal("100")
        else:
            rs = avg_gain / avg_loss
            rsi = Decimal("100") - Decimal("100") / (Decimal("1") + rs)

        oversold_d = Decimal(str(oversold))
        overbought_d = Decimal(str(overbought))

        if rsi < oversold_d and not in_position:
            in_position = True
            buy_price = closes[i]
        elif rsi > overbought_d and in_position and buy_price is not None:
            pnls.append(closes[i] - buy_price)
            in_position = False
            buy_price = None

    return pnls


def _evaluate_rsi_params(
    closes: list[Decimal],
    rsi_period: int,
    oversold: int,
    overbought: int,
) -> Decimal:
    """Pseudo-Sharpe: mean(PnL) / std(PnL) over simulated trades."""
    pnls = _simulate_rsi_trades(closes, rsi_period, oversold, overbought)
    if not pnls:
        return Decimal("-Infinity")

    n = Decimal(len(pnls))
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / n

    if var == 0:
        return Decimal("+Infinity") if mean > 0 else Decimal("-Infinity")

    try:
        std = var.sqrt()
    except Exception:
        return Decimal("0")

    return mean / std if std != 0 else Decimal("0")


__all__ = ["AdaptiveRsiStrategy"]
